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
| D3E-B | the managed worker unit `isadoraair-active-message-worker.service` (start gate, bounded restart/stop), Monitoring of it, the next protected generation's signed-policy entry (unsigned source only; deployed by r0116) | **no** — gate still closed |
| D3E-C | retention and reconciliation: a read-only reconciler, one eligibility authority, safe cleanup of provably unreferenced leftovers, explicit operator-driven publication retirement (`manage.py active_message_retention`) | **no** — gate still closed; nothing selected by age |
| D3E-D | read-only board diagnostics (reconciliation, worker health as Monitoring validated it, publication / retirement / air evidence), long-session status refresh, the r0116 readiness checklist (docs/releases/r0116_readiness.md) | **no** — gate still closed; nothing on the board changes state |

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
`rendering`, after `checked_at`, at most 3750 s later). Relations, from the
writer's own state rules: unless `previous` is `unclean`, `recent_starts` is
exactly `[started_at]`; `polling` needs `cycle >= 1`; `starting` has
`checked_at == started_at`; before the first completed cycle there is no
result, code, counter or `last_progress_at`, and an `execution` only while
`rendering` (a render during the first pass). Booleans are never accepted as
numbers; NaN/Infinity are refused. The strict decoder
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

**D3E-B** packages this contract as a managed unit and a Monitoring check --
see "Managed worker unit and Monitoring (D3E-B)" below.

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

## Managed worker unit and Monitoring (D3E-B)

### The unit

`deploy/isadoraair-active-message-worker.service` runs
`manage.py active_message_worker` (the D3E-A loop) as the station account
(`@@ISA_USER@@`, never root) from the application root with the station
`.env`. With the native-airing gate closed it materializes the one due
occurrence, has `isadoraair-render` render the exact aggregate and binds it
(PENDING_RENDER → READY) -- and stops there: no publication, no UrgentPA
Track, no engine command, no LogItem/PlayEvent, no ACCEPTED/CLAIMED/STARTED.
OGRemote remains the authority for remote producers; the unit does not name,
order against or touch OGRemote, the engine, Weather/AMBER or the encoders.

| Setting | Value | Why |
| --- | --- | --- |
| `After=` | network, `postgresql`, `isadoraair-render`, `isadoraair-validation` | start after its dependencies when they are present |
| `Wants=` | `isadoraair-render`, `isadoraair-validation` | weak: a renderer outage is retryable work (`render_retry`, backoff, attention after 3), never a reason for the worker to stop. No `Requires=`/`BindsTo=` |
| Start gate | `ExecStartPost=manage.py active_message_worker_ready --pid ${MAINPID} --timeout 45`, `TimeoutStartSec=90` | below |
| `Restart=` | `on-failure`, `RestartSec=10` growing (`RestartSteps=4`) to at most `RestartMaxDelaySec=120` | a crash or failed start is retried forever, never in a tight loop; a clean exit (SIGTERM) is not restarted |
| Stop | `KillSignal=SIGTERM`, `TimeoutStopSec=30` | below |
| Hardening | `NoNewPrivileges`, empty `CapabilityBoundingSet`/`AmbientCapabilities`, `PrivateTmp`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK`, `RestrictRealtime`, `RestrictSUIDSGID`, `RestrictNamespaces`, `LockPersonality`, kernel module/log/tunable, clock and hostname protection, `SystemCallArchitectures=native` | no capability, privilege change or real-time scheduling. No file-system confinement (`ProtectSystem`/`ProtectHome`): the worker writes rendered media under the application's media root and its heartbeat in `/run/isadoraair`; PostgreSQL may be TCP or a Unix socket; the renderer is a Unix socket |
| Runtime directory | none (`RuntimeDirectory=` deliberately absent) | below |

`systemd-analyze security` rates the rendered unit 3.9 ("OK"; the spoken
worker's unit is 8.7).

### Heartbeat file: `/run/isadoraair/active_message_worker.json`

The directory is the SHARED one from `deploy/isadoraair-tmpfiles.conf`
(`d /run/isadoraair 0755 <station user> <station user> -`): created at boot by
`systemd-tmpfiles`, used by the engine, Monitoring, Liquidsoap, aircheck and
others. The unit does not own it (no `RuntimeDirectory=`, which would conflict
with the shared directory and delete the heartbeat -- and its restart history
-- on every stop) and never changes its ownership. The worker writes the file
`0600` (atomic replace, D3E-A); Monitoring runs as the same station account,
so it -- and nobody else -- can read it. The file survives worker restarts
and is cleared at boot.

### Start-up: process readiness, not domain progress

A start job (including Update Center's `enable --now`) completes only when
the gate (`active_messages.supervision.startup_failure`, read-only: the file
and the process table, never the database) sees, within 45 s:

* a heartbeat that validates under the D3E-A contract,
* written by THIS process: its `pid` equals `$MAINPID` and its `started_at`
  is not earlier than that process's start (less 2 s for whole-second
  clocks) -- a file left by an earlier process, even a healthy-looking one,
  never counts (a different pid, or, on pid reuse, a start before the new
  process existed),
* not a recorded stop, not future-dated, and
* the first pass finished (`cycle ≥ 1`) or a render begun (`rendering`).

Any first-pass outcome counts: idle (nothing active), `blocked`, a READY
aggregate waiting behind the closed gate, a retryable render failure, even a
database error (the process is up; Monitoring then warns). The gate never
needs a render to complete, an active message or the database. It fails --
and the start job with it -- if the process exits (immediately), cannot write
its heartbeat (missing/unwritable directory) or never completes a pass; the
unit then retries per `Restart=on-failure`.

### Stop and forced termination

SIGTERM ends the loop between passes or within one 0.5 s wait slice and
records a clean stop (`phase: stopped`, exit 0, not restarted). A render in
progress is not interrupted by SIGTERM; systemd kills the process after
`TimeoutStopSec=30` so an Update Center stop/restart is never held up by a
long render. The killed process leaves no clean-stop record; the next start
records `previous: unclean` and renders the same PENDING_RENDER execution
again (D3E-A crash boundary: reuse of a proven rendition, else a fresh
render). Three such unclean ends within 15 minutes read as a crash loop --
expected only if the unit is repeatedly stopped mid-render. An abnormal exit
(crash, OOM, SIGKILL) is restarted after the restart delay.

### Monitoring

An ordinary **Systemd Service** check of exactly
`isadoraair-active-message-worker.service`
(`monitoring/services/probes.py`, `_active_message_worker_readiness`): one
`systemctl show` (ActiveState, SubState, NRestarts, MainPID,
ExecMainStartTimestamp) plus `active_messages.supervision.observe`, which
reads the heartbeat with D3E-A's own decoder and verdict and accepts it only
when it belongs to the running process (MainPID and ExecMainStartTimestamp, as
the start gate does). Identity fails closed: PID equality alone never counts
(PIDs are reused), so without a usable start time the card is never `ok`. Read-only: a file read and, only if native airing could
ever be open, one SELECT of the gate; no policy row, execution, render,
expiry or engine command. An observer failure is `unknown`
(`observer_error`, exception type only), never a Monitoring crash.

| Condition | Monitoring | reason |
| --- | --- | --- |
| active, fresh current heartbeat, idle | ok | `idle` |
| READY waiting behind the closed native-airing gate | ok | `ready_waiting_for_gate` |
| render in progress within its deadline | ok | `rendering` |
| retry backoff; playout in flight; progress | ok | `retry_backoff` / `playout_in_flight` / `working` |
| worker lock held elsewhere (12 busy passes) | warning | `lock_held_elsewhere` |
| composition permanently blocked | warning | `blocked` |
| database failure (1–2 in a row) | warning | `database_errors` |
| 3 database failures in a row | critical | `malfunction` |
| unverifiable previous restart history | warning | `history_unverified` |
| automatic restart since the previous poll (heartbeat otherwise ok) | warning | `restarted` |
| stale, malformed or future-dated heartbeat | critical | `stale` / `malformed` (`problem` says how) |
| render past its deadline | critical | `render_overdue` |
| ≥ 3 unclean starts within 15 minutes | critical | `crash_looping` |
| failed / stopped / between automatic restarts | critical | `service_failed` / `service_inactive` / `restarting` |
| active but the heartbeat records a stop | critical | `stopped` |
| start gate running; or no current heartbeat within 90 s of the process start | unknown | `starting` |
| no heartbeat after that grace | critical | `no_heartbeat` |
| a heartbeat from another process after the grace | critical | `heartbeat_not_current` |
| no MainPID, or no usable process start (`ExecMainStartTimestamp` missing, empty or unparseable): the writer cannot be proven | unknown | `identity_unavailable` |

A long-waiting ACCEPTED/CLAIMED airing is never itself a failure, and the
closed gate never degrades the status (`native_airing: closed` is shown). The
existing debounce (`consecutive_failures_required`, default 2), transition
events and notifications apply unchanged. The card shows the state, uptime,
the last heartbeat's age, phase, last outcome and code, the execution (first
8 characters), the gate and the automatic-restart count -- no path, pid
history or raw document. **No Restart button**: the unit is not in the
protected operator-restart allowlist, and Monitoring never restarts it.

The check is configuration, not schema, provisioned unattended by migration
**`monitoring.0014_provision_active_message_worker_check`** (one `RunPython`,
no schema change, reverse a no-op):

* the worker's check is identified ONLY by kind `systemd` + the exact unit
  `isadoraair-active-message-worker.service` -- never by its name. If one
  exists (any name, enabled or not, customized) every field is left exactly
  as it is; re-running never duplicates it;
* otherwise exactly one check is created -- enabled, default
  debounce/notifications, shown, sorted after the other systemd checks --
  under the first free name of a fixed, bounded series: "Active Message
  Worker", then the unit name `isadoraair-active-message-worker.service`, then
  "Active Message Worker (2)" ... "Active Message Worker (100)" (all within
  the 100-character name limit). A row holding one of those names is never
  touched, renamed or adopted;
* an absent check is therefore either created or the migration FAILS: if all
  101 names are taken by other checks it raises `CandidateNamesExhausted`
  (rolled back, not recorded as applied), naming the fix -- rename one of
  those checks or add a Systemd Service check for the unit under any free name
  in Django admin -- after which the update is retried. A database error
  (e.g. a concurrent name collision) is never swallowed either;
* reverse is a no-op (a check an operator may have customized is never
  deleted);
* it touches no service, unit or Active Messages row.

The executor applies migrations before unit reconciliation and restarts, so
the check exists before the worker starts and before Monitoring restarts. In
the seconds between the migration and the unit's `enable --now` the running
Monitoring may see the unit absent; the default two-poll debounce absorbs a
short gap, and if the job stops before activating the unit, the critical card
is the truthful signal (the release did not finish).
`manage.py provision_active_message_worker_check` (idempotent, `--dry-run`)
and Django admin remain repair paths.

### Expected healthy dormant state

Active (running), card **ok** with `idle` -- or `ready_waiting_for_gate` once
a message has been published and rendered -- and `native airing closed`;
the heartbeat refreshes at least every 5 s while idle; no automatic restarts;
no executions beyond READY.

### Protected deployment (r0116) -- authorization, not a manual install

The worker becomes a managed unit only through the signed protected policy:
D3E-B adds exactly `isadoraair-active-message-worker.service: ENABLE_NOW` to
`deploy/updater_runtime/protected-policy.json` -- the policy SOURCE of the
next generation (15), unsigned. The generation-14 runtime refuses the unit
outright (its signed policy decides membership: install allowlist,
`UNKNOWN_MANAGED_UNIT` for an ordinary plan, restart allowlist); only a
release that carries a verified generation-15 candidate whose signed policy
names it, and whose manifest declares it, can install it
(`verify_new_units_authorized_by_candidate_policy`). Proved in
`updatecenter/tests/test_active_message_worker_policy.py`.

**Runtime 13 -- release-driven restart authority** (the r0112 spoken-worker
precedent; `test_active_message_worker_restart.py`). Generation 15 also
carries protected runtime 13, which adds the exact basename
`isadoraair-active-message-worker` to the protected `CORE_SERVICES` and to the
Django manifest's `CORE_RESTARTABLE_SERVICES`, and to both identical
`RESTART_ORDER` tables:

    gunicorn, engine, monitoring, spoken-render-worker,
    active-message-worker, encoders, rbds

(the two application workers each depend only on PostgreSQL, the renderer
and the validator, and nothing depends on them; every other service keeps its
relative order). A LATER release that changes worker code declares
`isadoraair-active-message-worker` in `services_requiring_restart`; the
updater restarts exactly it (through its start gate) and nothing else. An
undeclared restart never happens; a changed unit template without a
declaration is installed and reloaded, not restarted (the existing rule);
unknown names, `.service` suffixes, wildcards and out-of-order lists are
refused. No release-manifest schema, daemon wire or bootstrap protocol
changed. As with r0112, `CORE_SERVICES` also seeds the compiled fallback map
(`MANAGED_UNIT_POLICIES`, the generation-1 document) with the worker
`ENABLE_NOW` -- the same value as the generation-15 policy source; that map
is consulted only with NO active signed policy, never on a Phase-D station.
This is release-driven authority only: the worker is NOT in the
root-configured operator-restart allowlist, and the Monitoring card keeps no
Restart button.

**Bridge rule:** a runtime-12 worker parses every manifest in the chain before
its handoff and refuses the new name, so r0116 (which delivers runtime 13)
must NOT declare the worker restart -- nor does it need to: `enable --now`
starts the newly installed unit on r0116 code, once. Only releases after
r0116 may declare it.

The r0116 packaging (not D3E-B) must:

1. build the generation-15 descriptor from the reviewed runtime tree
   (`deploy/updater_bootstrap/tools/protected_runtime_release.py`,
   `build_descriptor`, runtime version 13, manifest protocol 5, wire 3/4),
   whose changes from generation 14 are exactly the policy entry and the
   runtime-13 restart authority (`release.py`, `__init__.py`);
2. sign the primary-release statement offline with the real signer and verify
   it against the trusted public key; commit the manifest, descriptor and
   attestation atomically in the release-introduction commit (the r0109
   pattern), and validate with `updatecenter.protected_release_validator`;
3. declare the unit in **`systemd_units_new_required`** (not `_optional`:
   optional units are report-only and never installed by the updater, which
   would leave a manual install -- exactly what the signed path exists to
   prevent), with `isadoraair-monitoring` (new probe) and
   `isadoraair-gunicorn` (dashboard, manifest validator, planner) -- never
   the worker itself (bridge rule) -- in `services_requiring_restart`;
4. declare `monitoring.0014_provision_active_message_worker_check` in
   `migrations_required` (`migration_compatibility: "additive"`). Its one
   `RunPython` is classified **manual** by Update Center (never added to the
   automatic allowlist), so the release needs its trusted migration-
   authorization companion -- authored only AFTER the release-introducing
   commit exists (the r0111 / r0115 procedure):
   1. finalize and commit the r0116 release-introducing commit (manifest,
      descriptor, attestation); record its full SHA `R` and
      `sha256(deploy/releases/r0116.json at R)` `M`;
   2. confirm the migration bytes at `R` are the reviewed ones --
      `sha256(monitoring/migrations/0014_provision_active_message_worker_check.py at R)`
      must be `e87ae5de003d01c857884821106b0d7a618d15e926ec84820fcf072e4a245566`
      (the collision-safe version; the superseded `5d10a044…` bytes must
      never be authorized) -- and that the real classifier
      (`updatecenter_probe`) reports exactly one manual operation: index 0,
      `RunPython`;
   3. write `deploy/migration_authorizations/r0116.json` -- schema 1,
      `release_id` r0116, `target_commit` R, `manifest_sha256` M, and exactly
      one `authorized_manual_operations` entry (that ref, that file SHA-256,
      `operation_index` 0, `RunPython`, `manual`), `json.dumps(..., indent=2)`
      plus a newline;
   4. commit it alone, as a single-parent child of R that changes only that
      file (metadata-only); never amend R, never modify the companion later;
   5. validate with both validators (the protected runtime and
      `updatecenter.migration_authorization`) at the new tip, and publish
      both commits together. No real values exist before step 1 -- none are
      invented in D3E-B (`test_active_message_worker_migration` proves the
      contract with synthetic commits);
5. after installation: the "Active Message Worker" card present (from the
   migration) and `ok`, the heartbeat current, the gate still closed, and a
   fresh Runtime Recovery payload for generation 15 (as for every generation
   change).

The updater then installs the template, reloads once, `enable --now`s it --
the start job completes only through the start gate -- and verifies it
active. **Failure:** a failed start fails the job (`SAFE_EXECUTION_FAILURE`,
manual) before any declared restart; it never touches another unit. There is
no automatic rollback (docs/UPDATE_CENTER.md): generation 14 stays the
last-known-good slot, the schema and checkout are the release's, and the unit
stays installed and enabled, retrying with backoff -- harmless with the gate
closed. Fix forward with a corrected signed release; never by hand-installing
or editing units, policy or trust configuration. Nothing in the rollout can
open native airing (`NATIVE_AIRING_AVAILABLE` is a source constant, False,
not configuration; the unit sets no environment) or stop OGRemote (no
OGRemote unit is in the updater's authority).

### Troubleshooting (safe while native airing is closed)

* **Stale heartbeat** (`stale`, or `heartbeat_not_current`): check
  `systemctl status isadoraair-active-message-worker` and its journal; a
  process wedged in a database call or a render past its deadline shows here.
  A restart through the normal administrative path (`systemctl restart`, not
  a dashboard button) is safe: the next start resumes the same execution.
* **Service failed / restarting**: the journal names the cause (settings,
  database, import error). A start-gate failure prints
  `not started after 45s: <reason>` -- `missing` / `unreadable` (heartbeat
  could not be written), `first_pass_pending` (the first pass did not
  finish), `previous_process`.
* **Missing runtime directory** (`/run/isadoraair` absent or not the station
  account's): it is provisioned at boot by the station's tmpfiles entry;
  `sudo systemd-tmpfiles --create` (the station's existing tmpfiles
  configuration) recreates it. Do not create a different directory or change
  the unit.
* **Render failures**: `retry_backoff` is normal while `isadoraair-render`
  is down (check its own card); a permanent failure leaves the composition
  `blocked` (warning) with a SystemEvent naming the cause -- change the
  message set on the board (clear, expire, replace audio).
* Safe actions: board operations, starting/stopping/restarting the worker.
  Never edit the heartbeat file, the protected policy, the trust
  configuration or installed unit files, and never bypass Update Center.

## Retention and reconciliation (D3E-C)

`active_messages/retention.py` is the ONE authority on what may be reclaimed
(and the only code that reclaims it); `active_messages/reconcile.py` is the
read-only reconciler; `manage.py active_message_retention` is the operator
interface. Retention reclaims **storage** (bytes on disk). It never deletes
or rewrites **history** (database rows).

### Ownership and reference matrix

| Artifact | Owner / references | D3E-C |
| --- | --- | --- |
| Producer message audio (`ProductionMedia`) | `ActiveBroadcastMessage.message_media` (PROTECT); frozen snapshots by identity | **retained** (messages are never deleted) |
| Reference / stinger audio | `ReferenceAudio` registry (PROTECT, current and retired) | **retained** (the registry's) |
| Bound aggregate (`mix_media`) | `ActiveBroadcastExecution.mix_media`, `ActiveBroadcastPublication.media` (PROTECT) | **retained** as history; reported (`terminal_mix_retained`) |
| Unbound aggregate (rendered, never bound) | nothing (crash between render and bind; composition moved on) | **reclaimable**: bytes purged via Production's `purge_media` (row, hash, provenance kept), past the grace |
| Aggregate of a FAILED / SUPPRESSED / INTERRUPTED execution | the execution (bound) | **retained** like any bound aggregate |
| Published file `UrgentPA/am-<hex>.flac` | `ActiveBroadcastPublication` + its Track | **retirable** by an explicit maintenance action only; never while ANY Track owns the path |
| Library Track + waveform of a publication | the library (deleted through its own Collector) | removed **with** retirement only |
| Interrupted-publication leftovers (`.am-publish-*` scratch; a placed file with no publication row) | nobody (crash) | **reclaimable** by the sweep, past the grace; scratch never while a publish holds its lock; a placed file only when it is exactly its (non-READY) execution's aggregate and no Track owns the path |
| Destination-lock sidecar `.am-<hex>.flac.publish.lock` | the publication lock (every placement, retirement, cleanup) | **retained forever** (a persistent synchronization artifact: a small empty file per destination) |
| Publication / insertion correlation rows | the domain (append-only, never deleted) | **retained** forever |
| LogItems, PlayEvents, segments, execution events | library / domain history | **retained** (library retention is the library's) |

Nonnegotiable: frozen snapshots, execution identity / generation / digest /
state history, append-only events, publication and insertion rows with their
Track / LogItem id snapshots, actual-air evidence and confirmed durations,
and media provenance and hashes are never removed or rewritten. A missing
Track never means "never aired"; ACCEPTED or CLAIMED never means "aired"; a
PlayEvent stays authoritative after its Track or LogItem is pruned.

### Eligibility (`retention.assess_*`)

* **Execution** (necessary, never sufficient): terminal (COMPLETED, FAILED,
  SUPPRESSED, INTERRUPTED) with no open slot. PENDING_RENDER, READY,
  ACCEPTED, CLAIMED and STARTED are refused (`execution_<state>`) however old.
* **Publication retirement** additionally needs: not already retired; the
  Track (if it still exists) provably the publication's -- id snapshot,
  reserved path and candidate name (`track_not_publication` otherwise);
  no unresolved deck claim (`deck_claim_unresolved`) or open air evidence
  (`air_evidence_open`) on any LogItem naming the Track or any insertion's
  LogItem; deleting the Track would neither be refused (`track_protected`:
  playlist, rotation slot) nor cascade into anything (`track_has_dependents`:
  voice tracks, ...); the reserved namespace is a real directory under a real
  library root (`library_unavailable`, `namespace_not_a_directory`, ...); the
  published file is not a symlink or other non-regular entry. An unclaimed,
  unplayed LogItem is not a consumer: deck-open authority refuses every
  native LogItem that is not the current attempt of a live execution.
* **Aggregate purge**: an `active_message_mix` rendition, present, referenced
  by nothing (Production's own reference discovery), older than the grace;
  `purge_media` re-checks every reference under the media row lock.

Never authorized by: age alone, a missing Track, a terminal state alone, or a
path supplied by anyone.

### Retirement lifecycle

Published and available (live execution: dispatchable) -> historical (terminal
execution: never dispatched again; deck-open authority refuses it) ->
**retired** (operator action) -- distinct from "missing unexpectedly"
(integrity damage) and "pruned through the library" (expected).

`retire_publication` (one named execution):

1. outside any lock: assess (dry run stops here: `eligible` with the bytes it
   *would* reclaim);
2. one transaction, locks policy -> execution (the established order; the
   Track is not row-locked first): re-assess; append `publication_retired`
   (publication id, Track id snapshot, SHA-256, relpath, what was present) as
   the SYSTEM maintenance actor (`detail.via: maintenance_command`); delete
   the Track through the vetted `Collector` -- referrers
   SET_NULL (their snapshots stay), then the Track, the library's own order
   (a concurrent library deletion serializes with it; a dependent created
   meanwhile fails the deferred foreign key and rolls the retirement back);
3. after commit, under the publication destination lock: remove the file
   only if no Track of ANY id owns its path (aliases included) and it is a
   single-link regular file of exactly the publication's size and SHA-256
   (hashed through the opened descriptor, via a verified directory
   descriptor); the waveform only if no Track has that id or records that
   waveform. Both are removed ONLY through the identity-preserving quarantine
   below -- never by unlinking the shared pathname. Anything uncertain
   refuses and keeps the bytes (`path_owned_by_track`,
   `file_identity_mismatch`, `file_hard_linked`,
   `quarantine_identity_mismatch`, `ownership_unavailable`, ...).

**Authority.** Retirement through the command is a TRUSTED MAINTENANCE
operation of the station's operating-system boundary: whoever can run
`manage.py` holds the application's database credentials (the station
account's `.env`) and could change these rows directly. It is not an
authenticated per-user action -- the command accepts no username (there is no
`--operator`), records no person (the station account is shared), and records
the event as `ActorKind.SYSTEM` with `detail.via: "maintenance_command"`. An
authenticated per-user retirement (verified session, CSRF, the
`active_message.control` capability) does not exist in D3E-C.

**The destination lock.** `playout._destination_lock` is an `flock` on the
sidecar `.am-<hex>.flac.publish.lock`, which is PERSISTENT: it is never
unlinked -- not on release, not by any sweep, whatever its age. A process
that has already opened the sidecar and waits for it must lock the very
inode every later process opens; unlinking it (as the original helper and the
first D3E-C sweep did) let a waiter and a newcomer hold "exclusive" locks on
different inodes at the same time. The sidecar is opened without following a
symlink and must be a single-link regular file; after locking, the pathname
must still name the locked inode -- otherwise the operation fails closed.

**Track ownership of the files cleanup removes.** A Track refers to files
through TWO fields: `filepath` (its audio) and `waveform_path` (its
waveform). Cleanup never removes a file -- in its original place or held in
the quarantine below -- that any Track refers to through either field.

*Cleanup-time check* (`retention.track_references`, the one predicate every
removal path uses: retired audio, crash recovery, orphans, waveforms,
quarantine recovery, retirement assessment and the reconciler). Every Track
row is read through the base manager (nothing hidden), in bounded batches by
primary key; for each non-empty `filepath` and `waveform_path`:

* it refers to the OBJECT when `stat` of the stored path -- following every
  directory and file symlink, `.` and `..` -- reaches the object's very
  device and inode (a hard link is the same inode);
* it refers to the NAME when its fully resolved path is the file's home
  name (the reserved destination, or `WAVEFORMS_DIR/<id>.json`), whatever is
  there now;
* a path that resolves to nothing refers to no object (only its resolved
  name is compared); identical contents, an identical basename elsewhere or a
  directory-name substring are never ownership;
* a path that cannot be resolved (permission denied, a symlink loop, an
  invalid path) makes ownership UNKNOWN: `ownership_unverifiable`; a database
  error is `ownership_unavailable`. Unknown is never "no owner": nothing is
  removed, a quarantined object stays pending.

Answers: a reference to the name (or to the object at its name) refuses
(`path_owned_by_track` / `waveform_owned_by_track`; an object already held in
quarantine is put back under that name, never over an occupant); a reference
to an object held in quarantine keeps it exactly where it is
(`quarantine_owned_by_track`, pending -- moving it would break that Track).
For a waveform, a Track whose id is the waveform's name also owns it.

*Write guard* (`active_messages.track_guard`, registered through
`library.register_track_write_guard` as spoken's projection guard is). It
judges every write of EITHER field on every supported ORM path: save,
create, update_or_create, bulk_create (including the
`update_conflicts=True` upsert), QuerySet.update and bulk_update.

1. *Literal values only.* A path field may be written as a literal: a string
   (or path object), or `Value("...")`, which is judged exactly as that
   string. Any other ORM expression assigned to `filepath` or
   `waveform_path` -- `F(...)`, `Case`/`When`, `Concat`, `Cast`, `Coalesce`,
   combinations, `Subquery`, `RawSQL`, a `Value` wrapping an expression -- is
   REFUSED: the database computes it when the statement runs, so its target
   cannot be authorized beforehand (the guard never evaluates an expression
   against guessed or in-memory values, and never pre-queries a value the
   statement might then compute differently). The one recognized shape is the
   statement Django's own bulk_update issues -- `Case(When(pk=<int>,
   then=<literal>), ...)`, cast to the field's type on PostgreSQL -- whose
   every literal lands only on the row with that primary key, and is judged
   per row. Expressions on all other columns are unaffected. (Independently
   reproduced bypass: `update(waveform_path=Value(payload))` committed while
   cleanup sat at its final checkpoint, then cleanup deleted the payload --
   `GuardBypassRaceTests.test_p1a_*`.)
2. *Where it resolves* (directory and file symlinks, `.`, `..`):
   * into a cleanup quarantine: refused, always (nothing legitimately refers
     to a quarantined object; no lock to race);
   * to a reserved `UrgentPA/am-<hex>.flac`, or to a publication's waveform
     name `WAVEFORMS_DIR/<track_id_snapshot>.json`: permitted only while the
     writing thread holds that publication's destination lock, and only if
     the file is present NOW -- re-validated under the lock, so a writer that
     waited for a cleanup never commits a reference to what it removed. The
     holder must commit before releasing the lock (publish registers its
     Track in `atomic(durable=True)` inside it);
   * the single lock-free exception: an UPDATE of the live publication's own
     Track -- the row the publication's foreign key still points to (the key
     of a retired or library-pruned publication is NULL) -- writing that
     publication's own file name (re-analysis, an edit saved in full). Such a
     write cannot create a row: `Track.save` is forced to be an update (the
     guard returns `library.models.UPDATE_ONLY`), bulk_update only updates,
     and a QuerySet.update qualifies only when it filters on exactly that
     primary key. A Track id merely EQUAL to `track_id_snapshot` authorizes
     nothing: the snapshot is historical evidence, and a Track recreated with
     it (`create(id=snapshot, ...)`, `update_or_create`, bulk_create, an
     upsert, or a stale instance whose row was deleted) is refused without
     the lock (independently reproduced bypass:
     `GuardBypassRaceTests.test_p1b_*`).

A refused write raises `library.models.TrackWriteRefused` before anything is
written (the Track row, the file, publication and execution history and air
evidence are unchanged). Paths elsewhere, empty values and other columns are
untouched; ordinary library imports, analysis, recategorization, spoken /
Weather / Aircheck publication and CD ripping never write a protected name.
The library schema is unchanged.

*Why a concurrent writer cannot claim an object between the last check and
its deletion.* Cleanup holds the destination lock from before its final
ownership scan (the `authorized` checkpoint) until after the verified
deletion. In that interval a reference resolving into the quarantine, or any
expression, is refused unconditionally; a reference to the object's former
name needs the very lock cleanup holds, so the writer waits and, once it has
the lock, finds the file gone and is refused; an update of the live
publication's own Track cannot exist for a retired publication (its key is
NULL). A writer that held the lock first committed before releasing it, and
cleanup's scan sees its reference and refuses. All orders are tested across
PostgreSQL sessions with barriers (`GuardBypassRaceTests`,
`QuarantineClaimRaceTests`). Lock order is unchanged: a writer takes the
destination lock before opening its transaction and commits before releasing
it; cleanup holds no database row locks while it holds the destination lock.

*Existing references and the trust boundary.* The guard does not replace the
scan: references written through Django's base QuerySet, raw SQL, legacy
rows, or a symlink created after the write are found by the scan and keep
the object. Those bypasses are not reachable from station workflows; they
belong to privileged writers of the station account, which Django's
application hooks do not govern. Only such a writer, changing the filesystem
or the database outside the application in the instant between the final
scan and the deletion, is outside what an application lock can control.

### Identity-preserving removal (quarantine)

**Why "verify the pathname, then unlink it" is unsafe.** `unlink(name)`
removes whatever object the NAME denotes at the instant of the call. The
first D3E-C cleanup verified an opened descriptor (contents, size, inode),
re-checked with `stat` that the name still denoted that inode, and then called
`unlink(name)` -- a separate system call. A file renamed onto the name between
the two was the one deleted, and the verified file's length was reported as
reclaimed while the verified file survived elsewhere (independently
reproduced; preserved as
`test_retention_quarantine.FinalSyscallSubstitutionTests`). Another check
only moves the window: Linux cannot unlink a descriptor.

**Semantics** (`active_messages/quarantine.py`). Nothing D3E-C reclaims is
ever deleted through a shared name. Under the destination lock:

1. the caller re-establishes its authority (eligibility, retirement, and no
   Track referring to the name or to the object -- either field, any id, any
   alias: see "Track ownership" above);
2. the file is opened through the verified directory descriptor
   (`O_NOFOLLOW`) and proven to be a single-link regular file of the expected
   size and SHA-256;
3. a private operation directory `.am-quarantine/<name>.<128-bit random
   token>` is created with an exclusive `mkdir` (never an existing one) in the
   application's quarantine -- inside the same directory as the file (the
   reserved namespace, or `WAVEFORMS_DIR` for waveforms), so it is the same
   filesystem; mode 0700, owned by the station account, never a symlink
   (each is re-verified by descriptor);
4. an intent manifest (`intent.json`: device, inode, size, SHA-256) is
   written and fsync'd, then the directory;
5. the name is moved into the operation as `payload` with
   `renameat2(RENAME_NOREPLACE)` -- atomic on one filesystem, and refusing,
   never replacing, an existing destination; both directories are fsync'd;
6. the MOVED object is verified: it must be the manifest's inode, single
   link, the recorded and expected size and SHA-256;
7. the authority -- including the full Track reference scan for the moved
   inode -- is asked again; only then is `payload` removed from the
   private directory, and the removal is PROVEN by the verified descriptor's
   link count dropping to zero. Only that proven length is counted;
8. the manifest and operation directory are removed (fsync'd).

`payload` and `intent.json` carry no audio extension, so a library scan
(`import_songs`) never imports quarantined bytes, and the write guard refuses
any Track reference into a quarantine. A pre-existing reference (through
either field, by any alias) keeps the payload where it is
(`quarantine_owned_by_track`); a non-regular object found as `payload` is
never moved or deleted (`quarantine_recovery_required`). The persistent lock
sidecars are never touched.

**A replacement moves instead.** If another object took the name between
steps 2 and 5, THAT object is what moved. It is never deleted and never
counted: it is put back under its name with `RENAME_NOREPLACE` -- which can
only succeed while the name is free, so a newer occupant is never overwritten
-- and the cleanup reports `quarantine_identity_mismatch` (refused/failed, 0
bytes). If the name is occupied again, the object stays in quarantine and the
cleanup reports `quarantine_restore_blocked` for manual recovery; nothing new
is started for that name until it is resolved. The same applies when the
moved object is the right inode but was hard-linked (`file_hard_linked`) or
rewritten meanwhile, and when the authority withdraws after the move (e.g.
`path_owned_by_track`: the file goes back to the Track that now owns the
path). Availability exposure: a replacement is absent from its name only for
the instants between the move and the put-back (no hashing happens in
between when the inode differs).

**What is retained for manual intervention.** A payload whose identity cannot
be proven (manifest missing or invalid: `quarantine_recovery_required`), a
mismatched object whose name is occupied (`quarantine_restore_blocked`), a
payload a Track references, and anything unexpected in the quarantine
(`quarantine_entry_unexpected`, `quarantine_not_private`) are never deleted
automatically, whatever their age.

**Accounting.** Reported bytes are lengths whose removal was proven (link
count zero, single link before), counted by the run that proved them -- never
again by a retry or by recovery of an operation that already finished. A
hard-linked file is never removed (removing one name frees nothing). The
report distinguishes `quarantined bytes: quarantine_reclaimable` (verified,
awaiting deletion: a sweep removes them after re-proof) from
`quarantine_manual` (held for an operator).

**Supported filesystem and trust assumptions.** The namespace and its
quarantine are on one local POSIX filesystem supporting `renameat2(...,
RENAME_NOREPLACE)` (ext4, xfs, btrfs, tmpfs); where it is unsupported the
cleanup refuses before moving anything (`quarantine_unsupported`). The
destination lock coordinates every SUPPORTED writer of these names (publish,
the Track write guard, retention, recovery); it does not and cannot stop a
hostile process running as the station account, which could delete the files
directly. The private directory is never written by a supported writer; any
interference inside it is detected by the zero-link proof
(`quarantine_substituted`: nothing is claimed) rather than prevented.

### Deletion paths (audited in the final D3E-C correction)

| Path | Classification | Basis |
| --- | --- | --- |
| Retired publication audio (`retire_publication`, `finish_retirements`) | corrected: quarantine | identity re-proven on the moved inode; no shared-name unlink |
| Interrupted-publication orphan (`sweep_publication_namespace`) | corrected: quarantine | the execution's exact aggregate, re-proven on the moved inode |
| Retired publication waveform | corrected: quarantine (in `WAVEFORMS_DIR`) | the observed inode is the identity (derived data, no content authority); no Track has that id or path |
| Quarantine payload / manifest / operation directory | protected | unlinked only inside the exclusive 0700 directory, under the lock; payload removal proven by zero link count |
| Publication scratch `.am-publish-*` (`shutil.rmtree`) | protected by existing invariant | names are exclusive random `mkdtemp` names never reused; only their creating publish (holding its lock) writes inside; removed only past the grace with no publish lock held; `rmtree` walks by descriptor and never follows symlinks; no supported writer renames anything onto such a name |
| Destination-lock sidecars | never deleted | persistent synchronization artifacts |
| Unbound aggregate bytes (`purge_media`) | Production's accepted path, unchanged | content-addressed storage key of a row marked PURGED under its row lock; D3E-C only calls it |

A retired publication is never redispatched (its execution is terminal) and
is never reported as `publication_unavailable` damage. No migration: the
append-only event is the durable marker (as `reconciled` is for stinger
reconciliation).

### Crash recovery

| Interrupted | State afterwards | Recovery |
| --- | --- | --- |
| before cleanup / during assessment | unchanged | none needed |
| after eligibility, before the commit (incl. a failing Track deletion) | rolled back: no event, Track and file intact | retry |
| after the commit, before / during file removal | retired; file and/or waveform remain (harmless; nothing claims availability) | reconciler `retired_bytes_remaining` (in_flight); `--sweep --apply` (`finish_retirements`) removes them |
| after removal, before the report | retired; bytes gone | a retry reports `already_retired`, 0 bytes |
| quarantine: before the operation directory exists | unchanged | the next run starts afresh |
| quarantine: operation created, before / immediately before the move | file still at its name; an operation holding only a manifest | `quarantine_operation_leftover`; the sweep clears it (`abandoned_before_move`), then removes the file normally |
| quarantine: after the move / after verification, before deletion | file held as `payload`; the name is empty | `quarantine_pending` + `retired_bytes_remaining`; `--sweep --apply` re-proves identity (manifest inode, database SHA-256 and size) and authority from scratch, then removes and counts it once |
| quarantine: after deletion, before reporting | bytes gone; an operation holding only a manifest | cleared (`concluded`), 0 bytes (never counted twice) |
| quarantine: while putting back a mismatched object | the object held; its name empty | `quarantine_identity_mismatch`; the sweep puts it back (no-replace) |
| quarantine: put-back finds the name occupied | the object held | `quarantine_restore_blocked`; manual recovery, then `--sweep --apply` retries |
| quarantine: a recovery sweep is itself interrupted | one of the states above | the next sweep settles it the same way |
| quarantine: payload identity unprovable (manifest lost) | held | `quarantine_recovery_required`; never deleted automatically |
| another Track was registered at the path meanwhile (any id) | refused (`path_owned_by_track`); its audio kept | operator review |
| the file is not the publication's | refused (`file_identity_mismatch`); kept | operator review |
| the name was substituted before the move | the replacement is put back (`quarantine_identity_mismatch`), never deleted or counted | operator review |
| the file is hard-linked | refused (`file_hard_linked`); kept, 0 bytes | operator review |
| ownership lookup failed (database) | `failed` (`ownership_unavailable`); kept | retry |
| waveform removal fails (permission) | retired; file gone, waveform remains; outcome `failed` | `--sweep --apply` |
| a Track reappears for a retired publication | refused (`retired_track_present`), attention | operator review |

Bytes are removed only after the durable state commits; a publication never
claims bytes that are gone.

### Reconciliation (`manage.py active_message_retention [--deep]`)

Read-only: no message, execution, publication, Track, media or retention
state, file, policy generation (the policy singleton is never created),
retry counter or engine command changes; repeated runs give equal findings.
Bounded (`--limit` rows per class; namespace scan capped; truncation
reported). The metadata check stats files; only `--deep` hashes (never on the
engine's path).

| Code | Severity | Category |
| --- | --- | --- |
| `mix_media_bytes_inconsistent` | critical (live) / warning (terminal) | integrity_damage |
| `mix_provenance_inconsistent` | critical / warning | integrity_damage |
| `mix_media_purged_but_referenced`, `purged_media_still_referenced` | critical | integrity_damage |
| `pending_input_unavailable`, `active_message_media_unavailable` | critical | integrity_damage |
| `terminal_mix_retained` | info | retained |
| `aggregate_unbound` | info | candidate_retirement (eligible / blocked:too_recent / referenced) |
| `publication_digest_inconsistent`, `publication_track_snapshot_mismatch` | critical | integrity_damage |
| `publication_track_missing` (live) | critical | integrity_damage |
| `publication_file_missing`, `publication_track_path_mismatch`, `publication_file_digest_mismatch` (deep), `publication_file_not_regular` | critical (live) / warning (terminal) | integrity_damage |
| `publication_waveform_missing` | warning | integrity_damage (display only) |
| `publication_pruned` | info | expected_pruning (Track and file removed through the library) |
| `publication_file_without_track` | info | candidate_retirement |
| `publication_retained` | info | candidate_retirement (with eligibility / blocking reasons) |
| `retired_bytes_remaining` | info | in_flight (sweep finishes) |
| `retired_track_present` | warning | integrity_damage |
| `retired_path_owned_by_track`, `publication_path_adopted` | warning | integrity_damage (another Track owns the path; never removed) |
| `ownership_unavailable` | warning | unknown |
| `insertion_log_item_pruned` | info | expected_pruning |
| `current_insertion_log_item_missing` | warning | in_flight (the worker recovers) |
| `insertion_publication_mismatch`, `insertion_association_unexpected` | critical | integrity_damage |
| `namespace_orphan_file`, `publication_scratch_leftover` | warning / info | candidate_orphan (an orphan without a provable identity is refused: `identity_unverifiable`) |
| `namespace_file_awaiting_adoption` | info | in_flight (READY: publish adopts it) |
| `namespace_entry_unexpected`, `library_unavailable`, `namespace_not_a_directory` | warning / critical | unknown |
| `quarantine_pending` | info | in_flight (verified bytes held for deletion; the sweep re-proves and removes) |
| `quarantine_operation_leftover` | info | in_flight (holds nothing; the sweep clears it) |
| `quarantine_identity_mismatch` | warning | integrity_damage (not the verified file; the sweep puts it back) |
| `quarantine_restore_blocked` | warning | integrity_damage (manual recovery; never deleted) |
| `quarantine_recovery_required`, `quarantine_entry_unexpected`, `quarantine_not_private` | warning | unknown (manual recovery; never deleted) |
| `quarantine_owned_by_track` | warning | integrity_damage (a Track refers to the held object; counted as manual bytes; never deleted while it does) |
| `quarantine_name_owned_by_track` | info | in_flight (a Track refers to the held object's former name; the sweep puts it back) |
| `ownership_unverifiable` | warning | unknown (a Track path cannot be resolved; nothing is removed) |

The report prints `quarantined bytes: quarantine_reclaimable N,
quarantine_manual M` when anything is held: bytes a sweep may reclaim after
re-proof versus bytes that need an operator.

Destination-lock sidecars are counted as retained synchronization artifacts
(`healthy.publication_lock`), never candidates; a non-regular one is
`namespace_entry_unexpected`.

Monitoring attention is an explicit opt-in: the default report (and every
dry run) writes NOTHING -- no SystemEvent, no domain, Track, media or
retention row, no file. Only `--attention` records ONE deduplicated
SystemEvent per integrity / unknown code with warning or critical severity
(`active_messages|retention|<code>`, a count and five sample identities);
expected pruning, in-flight work, candidates, retained history and the
closed native-airing gate never alert.
No Monitoring worker or timer runs it; D3E-D can read the same structured
`Report` for the board.

### Operator commands

    manage.py active_message_retention                       # report (read-only; writes nothing)
    manage.py active_message_retention --deep                # report + hashes
    manage.py active_message_retention --attention           # report + record Monitoring attention
    manage.py active_message_retention --sweep               # dry run of the safe sweep
    manage.py active_message_retention --sweep --apply       # reclaim leftovers (grace >= 1 h; default 24 h)
    manage.py active_message_retention --sweep --apply --include-production   # + Production's own sweeps
    manage.py active_message_retention --retire <execution UUID> ...           # dry run: eligibility per execution
    manage.py active_message_retention --retire <UUID> ... --apply             # trusted maintenance action

`--sweep` first settles interrupted quarantine operations ("interrupted
cleanups (quarantine)": `removed` after re-proof, `restored`, `cleared`, or
`failed` when an operator is needed), then the namespace, retired-publication
and aggregate sweeps.

Defaults: read-only; destructive only with `--apply`; no timer, no
background cleanup, no age-based expiry of publications and no default
retention duration; at most 50 executions per `--retire`; never a path. Dry
runs report "reclaimable (not reclaimed)"; `--apply` reports bytes actually
removed. Exit 1 for a storage or database failure or any failed removal
(nothing is reported as reclaimed for it).

### Troubleshooting

* `retired_bytes_remaining` / a `failed` retirement: fix the cause (e.g. the
  library mount or permissions), then `--sweep --apply`.
* `quarantine_pending`: a cleanup was interrupted after moving verified bytes
  aside; `--sweep --apply` re-proves and removes them (or puts them back if
  authority changed).
* `quarantine_identity_mismatch` / `quarantine_restore_blocked`: something
  replaced a publication file while it was being removed. The replacement is
  in `UrgentPA/.am-quarantine/<name>.<token>/payload` (or under
  `WAVEFORMS_DIR/.am-quarantine/`). If its name is free, `--sweep --apply`
  puts it back. If blocked: compare the occupant of the name with the payload,
  move the occupant aside yourself, run `--sweep --apply` (it puts the payload
  back), then decide. Never delete a payload by hand without that comparison.
* `quarantine_owned_by_track` (a sweep reports `failed: quarantine_pending,
  quarantine_owned_by_track`, 0 bytes): a Track's `filepath` or
  `waveform_path` refers to the held object, directly or through an alias.
  Nothing is deleted and the Track is not changed. Decide in the library
  whether that Track should keep the file; if not, correct or remove its
  reference there (a separate, authorized library action), then run
  `--sweep --apply`: the object is re-proven and removed, and its bytes are
  counted once. Repeated sweeps while the reference remains change nothing.
* `ownership_unverifiable`: some Track's path cannot be resolved (permission
  denied, symlink loop). Fix or correct that path; until then no cleanup
  proceeds (an unresolvable path could be an alias of the file).
* `quarantine_recovery_required` / `quarantine_entry_unexpected`: an object
  whose identity cannot be proven; it is never deleted automatically. Inspect
  it; nothing else is affected.
* `library_unavailable` / `namespace_not_a_directory`: the library mount is
  absent or the reserved directory was replaced (e.g. by a symlink); nothing
  is removed until it is a real directory again.
* `publication_track_missing` on a live execution: the worker fails it
  (`publication_unavailable`); a terminal one with the Track gone is expected
  pruning.
* Blocked retirement: the reasons name the fix -- remove the Track from the
  playlist / rotation, wait for the deck's evidence to become terminal, or
  leave it retained.
* Safe while native airing is closed: every report and dry run; `--sweep
  --apply`; retiring terminal executions. Never edit or delete publication,
  insertion or event rows, never hand-delete files in `UrgentPA/am-*` or
  in a `.am-quarantine`.

### What D3E-C deliberately does not reclaim (Phase G dependencies)

Bound aggregates stay as long as their executions reference them (forever:
executions are history). With the gate closed this is bounded (one unfinished
execution; identical compositions reuse one rendition). Before Phase G airs
an aggregate every cadence, the station needs an explicit, separately
authorized aggregate-retention design (e.g. which terminal executions' bytes
may go while their rows stay) -- not age-based deletion, and not by removing
a foreign key. Likewise there is no automatic publication retirement policy:
any future one must be designed, authorized and qualified as such.

## Operator diagnostics and long sessions (D3E-D)

D3E-D adds read-only diagnostics to the existing Operator Board and makes its
live status reliable for a whole operator shift. Nothing here is deployed by
D3E-D itself: it ships with the integrated r0116 release
(docs/releases/r0116_readiness.md). The native-airing gate stays closed.

### Board diagnostics (`/iportal/messages/board/diagnostics/`)

CONTROL only (the board's `_require_control`; staff/superuser through the
established compatibility bypass); GET only (POST/PUT/DELETE: 405); every
response `Cache-Control: no-store`. The page has no form and no button that
changes anything: it never retires a publication, deletes media or a Track,
purges ProductionMedia, restores or removes a quarantine payload, runs
`--apply`, records Monitoring attention, or alters a retry or recovery state.
Maintenance stays with the trusted `manage.py active_message_retention`
command.

* **Inspection on request, never cached.** A plain visit says "No inspection
  has been performed" -- the absence of findings is never shown as health.
  "Inspect now" (`?inspect=1`) runs D3E-C's reconciler once
  (`active_messages.diagnostics.inspect` → `reconcile.reconcile`), bounded to
  `DIAGNOSTIC_LIMIT` (100) rows per class and metadata only (`deep=False`:
  no SHA-256 pass from the web; `--deep` stays a CLI diagnostic). No request
  parameter changes the bound or the depth. The result states when it was
  generated and that it describes that moment, not now; there is no
  background scheduler and no stored result.
* **States.** `complete` (every class fit the bound and nothing was
  unverifiable), `incomplete` (a class hit the bound -- named -- or something
  could not be verified; the counts cover only what was inspected), and
  `unavailable` (the inspection itself failed: database or storage; nothing
  is known, which is never a healthy result -- only the error class is shown).
* **Summary.** Counts by severity, category and operator class; integrity
  damage; cleanup / recovery candidates; items needing manual attention;
  quarantine bytes reclaimable after re-proof versus held for manual
  recovery.
* **Findings** (at most `MAX_FINDINGS`, 200, listed; counts cover all): the
  severity, D3E-C's condition code, a plain-language title, the reconciler's
  own evidence and suggested action, the class's interpretation, and a link
  to the existing airing or message page when the identity names one that
  exists (execution, publication, placed file, insertion attempt, quarantine
  operation). No server path or exception text is shown; all text is escaped.
* `/iportal/messages/board/diagnostics/data/` -- the same as JSON
  (`inspection`, `status`, `worker`, `native_airing`). It follows the SAME
  opt-in as the page: without it, `inspection` is null and `status`
  "not_inspected" (never a healthy result); with it, `status` is the
  inspection's `complete` / `incomplete` / `unavailable`, inspected afresh.
* **Explicit opt-in, one rule for both routes** (`board.inspection_requested`):
  exactly one `inspect` parameter whose value is exactly `1`. A missing,
  empty, `0`, `false`, `true` or any other value, a duplicated or
  contradictory parameter (`inspect=1&inspect=0`), or unrelated parameters
  never inspect -- no reconciliation, no Track ownership scan, no stat of the
  library. Authorization is checked first: an anonymous user, a producer or
  an expired session gets nothing, even with `inspect=1`. The status poll
  never inspects.

**Condition classes** (`diagnostics.classify`: D3E-C's codes first, then its
categories -- no definition of its own):

| Class | From | Operator response |
| --- | --- | --- |
| Integrity damage | category `integrity_damage` (incl. `quarantine_identity_mismatch`) | stored evidence contradicts the records; nothing changes automatically; review with the maintainer before any maintenance |
| Manual recovery required | `quarantine_restore_blocked`, `quarantine_recovery_required`, `quarantine_owned_by_track`, `quarantine_entry_unexpected`, `quarantine_not_private` | never deleted or moved automatically; follow the quarantine troubleshooting above |
| Unknown / unverifiable | category `unknown` (`library_unavailable`, `ownership_unavailable`, `ownership_unverifiable`, ...) | nothing is removed while unknown; fix the cause and inspect again |
| Quarantine pending | `quarantine_pending`, `quarantine_name_owned_by_track`, `quarantine_operation_leftover` | an interrupted cleanup; `--sweep --apply` re-proves and finishes it |
| Work in progress | category `in_flight` | the worker or a retry settles it |
| Cleanup candidate | category `candidate_orphan` | a sweep may reclaim it after the grace period |
| Retirement candidate | category `candidate_retirement` | only an explicit `--retire` maintenance command, never automatic |
| Expected historical pruning | category `expected_pruning` | the library's own retention; snapshots keep the history |
| Healthy / retained | category `retained` | deliberately kept history |

**Resource bounds.** The reconciler's per-class limits apply. Its Track
ownership checks for all inspected publications now run as ONE bounded scan
of the Track table (`retention.references_by_location`, the same rules as
cleanup's per-location `track_references`, proven equal in
`test_board_diagnostics`) instead of one full scan per publication. Cleanup
decisions are unchanged: they still use `track_references` under the
destination lock. Routine status polling never inspects.

### Worker health on the board

The strip's worker chip and the diagnostics page show the managed worker as
Monitoring LAST VALIDATED it -- D3E-B's probe binds D3E-A's heartbeat to
systemd's MainPID and ExecMainStartTimestamp (`supervision.observe`) -- read
from Monitoring's own state file. The board never reads the heartbeat file as
health, never infers a process from a PID, never calls systemctl, and offers
no restart control (Monitoring keeps no Restart button for this unit either;
the runtime-13 restart authority stays release-driven).

| Shown | Meaning |
| --- | --- |
| Worker check not provisioned | no Monitoring check of the unit (before r0116's `monitoring.0014`) |
| Worker check disabled | the check exists but is off: not validated |
| Monitoring status unavailable | no readable Monitoring state: unknown |
| Not yet observed by Monitoring | the poller has no result for the check yet |
| Monitoring status out of date | Monitoring's last poll is older than its own 30 s bound: the verdict is only the last known one |
| Worker OK / warning / critical / unknown — *condition* | Monitoring's verdict and condition |

**Untrusted Monitoring state fails closed.** The document is validated where
it enters the read model (`diagnostics.worker_health`), before any verdict is
read, and the result is "Monitoring status unavailable" -- unknown, never
healthy -- with a `problem` and a plain explanation (no traceback, no path):

* `unreadable` -- missing, unreadable, a symlink, over the size cap, not
  JSON, partially written, or a root that is not an object;
* `timestamp_missing` / `timestamp_invalid` -- Monitoring writes `timestamp`
  as `time.time()`, finite epoch seconds (UTC by definition): anything else
  (null, a string -- ISO, naive or numeric --, a boolean, an object, NaN,
  ±Infinity, or outside the heartbeat contract's 2000..2100 window) is
  refused, never converted;
* `timestamp_in_future` -- an observation newer than now cannot describe the
  present: it is refused, never clamped to age 0 (a negative age is never
  shown, and a serialized "ok" from the future is not current health);
* `checks_invalid` -- `checks` missing or not a list, or any entry that is not
  an object with an integer `id`, or a duplicated id (a garbled list must not
  read as "not yet observed"); an empty list is valid: "not observed";
* `entry_invalid` -- the worker's entry is incomplete, mistyped or contradicts
  the producer's contract (below); `field` names the part to blame
  (`status`, `detail`, `detail.reason`, ...) and the chip reads "Monitoring
  status unavailable — worker check data is incomplete".

Any unforeseen fault is also "unavailable" (`observer_error`), never a 500 and
never health.

**The worker entry's schema is its producer's.** The worker check has one
producer: `probes.probe_systemd` → `_active_message_worker_readiness` →
`supervision.observe` → `heartbeat.assess`, serialized by the poller's
`_build_result` (`{"id", "name", "kind", "status", "detail", ...}`). The board
validates exactly that contract, state by state, and defaults nothing that a
verdict depends on: a missing `detail` is not `{}`, a missing `active_state`
is not "idle", a missing `reason` is not a healthy condition, and no identity
or heartbeat evidence is ever synthesized.

| Producer path | `detail` the producer writes | Required by the board |
|---|---|---|
| systemctl could not run, or the probe raised | `{"error": "..."}`, status `unknown` | `error` (text), status `unknown`, no `reason` |
| unit not active | `active_state`, `sub_state`, `n_restarts`, `reason` (+ `uptime_seconds`, `restart_via_dashboard`) | all four; `reason` must be the one the producer derives from `active_state`/`sub_state` (`starting`, `restarting`, `service_failed`, `service_inactive`) |
| active, observer raised | as above, `reason: observer_error` (+ `error`) | the four; status `unknown` |
| active, heartbeat not bound to the process | + `native_airing`; `reason` `identity_unavailable` / `starting` / `no_heartbeat` / `heartbeat_not_current` / `malformed` (+ `problem`) | + `native_airing` (`open`/`closed`); `problem` (text) for `malformed` |
| active, heartbeat bound (`supervision.belongs`) | + `phase`, `outcome`, `code`, `execution`, `heartbeat_age_seconds`; `reason` = `heartbeat.assess`'s condition, or `restarted` | all of them, as one consistent observation (below) |

Optional display metadata (`uptime_seconds`, `restart_via_dashboard`,
`problem` text) never decides anything. A reason the poller never debounces
must be written under its own status (an `ok` condition `ok`; `starting`,
`identity_unavailable`, `observer_error` `unknown`); a unit-state reason must
be the one `active_state`/`sub_state` give; unit-state and unbound verdicts
carry no heartbeat reading (`phase`, `outcome`, `code`, `execution`, age) --
the probe returns before `observe` reads one -- and an inactive unit no
`native_airing`; `restarted` needs `n_restarts` >= 1 (the probe saw it rise).

**A bound heartbeat is one observation, validated as the producer wrote it.**
Monitoring serializes only `phase`, `outcome` (the heartbeat's `result`),
`code`, `execution` and the rounded age -- not `cycle`, `repeats`,
`previous`, `recent_starts` or `render_deadline` -- so the board enforces
exactly the producer's rules those fields can show:

* formats (`heartbeat.validate`): `code` is the whole string
  `[A-Za-z0-9_]{0,64}` (the producer's own pattern, full match); `execution`
  is `""` or a UUID in exactly the producer's canonical form
  (`str(uuid.UUID(x)) == x`: lowercase, hyphenated -- braces, `urn:`, no
  hyphens, upper case or padding are refused, never normalized); `phase` one
  of starting/polling/rendering/stopped; `outcome` one of the results or `""`;
* the writer's state (`Recorder`): `outcome` is `""` only before the first
  completed cycle -- then `code` is `""`, the phase is never `polling`, and an
  `execution` appears only for a render begun in that first pass; `starting`
  has no outcome; `rendering` always names its execution;
* the classification (`heartbeat.assess`, in its order): `stopped` exactly for
  the stopped phase; a crash loop in any other phase, within the crash
  window (below); in the rendering phase
  `rendering` (age within the longest deadline), `render_overdue`, or an ok
  render lifted to `history_unverified` / `restarted` -- never stale or an
  idle reading; otherwise `stale` exactly beyond the 60 s bound (both at the
  rounded 60.0), and below it the reason the outcome gives:

  | outcome | reason |
  |---|---|
  | `error` | `database_errors` or `malfunction` (the unserialized error count decides) |
  | `blocked` | `blocked` |
  | `busy` | `busy` or `lock_held_elsewhere` (the unserialized repeat count decides) |
  | `backoff`, `retry` | `retry_backoff` |
  | `playout` | `playout_in_flight` |
  | `ready`, `reused`, `failed`, `superseded` | `working` |
  | `idle` with code `gated` | `ready_waiting_for_gate` |
  | `idle` (other codes), `""` | `idle` |

  plus `history_unverified` / `restarted` for any ok reading.

**Time bounds on the serialized age.** Monitoring writes the heartbeat age as
`round(now - checked_at, 1)`. Every bound below is the producer's own constant
and comparison, read through that rounding: a true age just above a bound and
one at or just below it can both serialize to the bound itself, so the
rounded bound value stays possible either way, and one tenth beyond it is
excluded.

| bound (heartbeat.py) | producer comparison | serialized age: possible / excluded |
|---|---|---|
| crash window, `CRASH_WINDOW_SECONDS` = 900 | a start counts only if `0 <= now - start <= 900`; every start is at or before `started_at` <= `checked_at` | `crash_looping` up to 900.0; from 900.1 never |
| unverified history, same window | `0 <= now - started_at <= 900` | rendering: up to 900.0; `starting` (where `started_at == checked_at`): not below 0 |
| staleness, `STALE_AFTER_SECONDS` = 60 | stale when `now - checked_at > 60` (not while rendering) | fresh readings up to 60.0, `stale` from 60.0 |
| render deadline, `RENDER_DEADLINE_MAX_SECONDS` = 3750 | `checked_at < deadline <= checked_at + 3750`; rendering while `now <= deadline` | `rendering` up to 3750.0; `render_overdue` from 0.0 |
| future skew, `MAX_FUTURE_SKEW_SECONDS` = 30 | `checked_at > now + 30` is malformed (never bound) | down to -30.0; -30.1 refused |

So a crash loop reported with a heartbeat older than the window -- 900.1 s,
901 s, an hour -- is evidence the producer cannot write: no recorded start can
be inside the window when the record itself is outside it. It is
`entry_invalid` (`detail.reason/heartbeat_age_seconds`), identity and
heartbeat unknown -- never a verified crash-looping process, and not a stopped
or failed worker either. A crash loop at up to 900.0 s keeps its meaning
(critical, identity verified, heartbeat unknown). The producer grid includes
true ages on both sides of each bound and requires the reader to accept
exactly the producer's reasons at every serialized age. Monitoring's own
observation bound (its document is stale when `time.time() - timestamp > 30`)
is applied with the same strict comparison on the unrounded age.

`diagnostics.assessed_reasons(phase, outcome, code, age)` is that mapping. A
test classifies a grid of every valid heartbeat document with the producer's
own `validate`/`assess` and requires the reader to accept exactly the reasons
the producer produced for each combination of serialized fields, and to
refuse every other: a new or changed producer condition fails that test, and
reads as unavailable on the board, until this table is reviewed. Anything
else -- `idle` with an `error` outcome, `busy` with `idle`, `gated` without
the gated code, a malformed code or execution -- is `entry_invalid` naming
the relation (`detail.reason/outcome`, `detail.reason/phase`,
`detail.reason/heartbeat_age_seconds`, `detail.code/outcome`,
`detail.outcome/phase`, `detail.execution/phase`) or the field.

**A first-cycle render is legitimate, not incomplete.** The worker records a
render before its first loop cycle completes (`Recorder.rendering` at cycle
0), and the unit's start gate accepts it; the producer then writes `phase
rendering`, `outcome ""`, `code ""` and the render's canonical execution.
That is a healthy current render ("Worker OK — rendering", identity verified)
while within its deadline -- the empty outcome is accepted only with that
phase, no code and an execution, never for an incomplete entry (a missing
`outcome` key) or in any other phase.

What a valid entry establishes:

* **Process identity verified** -- only a bound-heartbeat condition (the
  verdict came past `supervision.belongs`), never under status `unknown`;
  **not current** for `heartbeat_not_current`; otherwise **unknown**.
* **Heartbeat current** -- only for conditions that passed assess's staleness
  bound (idle, busy, retry, playout, working, gated, blocked, lock held,
  database errors, malfunction, unverified history, restarted) and a render
  within its deadline. `stale` is **stale**; `stopped`, `crash_looping` and
  `render_overdue` are **unknown** (assess decided them before or without
  freshness); `no_heartbeat` is missing, `malformed` malformed.
* A **stale** Monitoring observation (older than 30 s) proves nothing current:
  identity and heartbeat are unknown; the last verdict is shown as such.
* The poller holds a first warning/critical result at the last stable status
  (the worker check needs 2 consecutive failures), so for one poll the status
  can read "ok" while the detail already says e.g. `stale` or
  `service_failed`. The board shows Monitoring's own verdict with that
  condition ("Worker OK — stale") and derives identity and heartbeat only
  from the detail -- never current or verified beyond what it proves.

**Unavailable is not unhealthy.** An untrusted or incomplete entry says
nothing about the worker: it is never shown as stopped, failed or critical,
and never as healthy -- the worker's state is unknown until the next
trustworthy observation. A genuinely failed, stopped or crash-looping worker
is a valid entry and keeps its real classification. Disabled, unprovisioned
and not-yet-observed checks are their own states and need no worker detail.
Timestamp validation and the explicit `?inspect=1` opt-in are unchanged. So the board, the status poll, the diagnostics page and its
JSON keep answering, and the rest of the status keeps updating; the next
trustworthy document restores the specific state. The existing staleness
bound is unchanged ("out of date" past 30 s).

Crash loops, automatic restarts, unverified history and restarting are
flagged as recovery. A healthy worker never means anything is on air.

### Historical evidence on an airing

The airing detail page adds "Publication and air evidence", classified with
`reconcile.inspect_publication` (the reconciler's publication rules for one
publication): never published · present · **retired by maintenance**
(the `publication_retired` event: time, `ActorKind.SYSTEM`, `via:
maintenance_command` -- an intentional retirement, never shown as a failure)
· retired with cleanup pending · retired, needs review · pruned through the
library · cleanup quarantine pending · missing or damaged · unknown. The
publication id, file name, SHA-256 and Track id snapshot, the insertion
attempts (token, LogItem id snapshot, whether that item was pruned), and the
deck's PlayEvents (times, played duration, evidence state, whether the Track
was since removed) are shown as recorded -- retirement of the Track or the
file hides or rewrites none of them -- and "confirmed air evidence" versus
"never aired" follows only the execution's STARTED evidence. Bounded: at most
`EVIDENCE_LIMIT` (50) insertions and play events; the page's query count does
not grow with history.

Navigation: findings link to airings and messages; the airing evidence links
to diagnostics. The existing message and airing history (keyset pagination,
deterministic order, bounded queries) needed no other change.

### Long-session status refresh

The strip, the ON AIR badges and the airing labels refresh from
`board/status/` (read-only; now also the worker chip):

* every 20 s while the tab is visible, with **no cutoff** (the D3D helper
  stopped after 90 refreshes, about 30 minutes);
* **at most one request outstanding**: the next refresh is scheduled only
  after the previous one settled; a forced refresh (tab shown again, a page
  restored from the back-forward cache) aborts the outstanding one; only the
  newest request's answer is ever applied;
* a failure or a 10 s timeout marks the page stale ("on-air indications may
  be out of date"); retries back off (40 s, 80 s, ... at most 5 min) and the
  first success restores the 20 s pace and clears the warning; no success
  for 60 s (a throttled or suspended tab) also marks it stale;
* a hidden tab sends nothing; showing it refreshes at once when the last
  answer is 20 s old or more; a back-forward restore re-enables its forms and
  re-checks at once, marked stale until answered;
* an expired session (401/403, a redirect to sign-in, anything that is not
  the JSON status) stops refreshing and says so -- it is never retried in a
  loop;
* **a stale page never asserts ON AIR as current**: ON AIR badges, the
  airing chip and on-air labels are dimmed and marked "(last known)"; ON AIR
  still means only STARTED with the engine's air evidence (ACTIVE, ACCEPTED
  and CLAIMED never are) and nothing is inferred from a healthy worker;
* rows are never re-rendered: expanded rows, focus, typed input, filters and
  the page stay; a changed board version shows "This page has changed" with a
  reload link to the same view (filter, search, page).

Deterministic browser tests with Playwright's fake clock
(`test_board_long_session_browser`) cover 120 refreshes, timeouts and
back-off, a 30-minute outage, session expiry, a hidden tab, a back-forward
restore, a version change, stale ON AIR and the worker chip.

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

## Release notes for D3E-B

* New unit template `deploy/isadoraair-active-message-worker.service` and its
  start gate `manage.py active_message_worker_ready`; not installed until a
  release declares it with the signed generation-15 policy (r0116).
* Protected policy source: one new entry
  (`isadoraair-active-message-worker.service: ENABLE_NOW`), unsigned; the
  protected-runtime descriptor still describes generation 14.
* Protected runtime 13: release-driven restart authority and order for
  `isadoraair-active-message-worker` (Django manifest/planner in lockstep);
  not an operator-restart permission.
* Monitoring identity fails closed: no usable process start time is
  `identity_unavailable`, never `ok`.
* Monitoring: the Systemd Service probe also reads `MainPID` and
  `ExecMainStartTimestamp` (every unit; other units' results are unchanged)
  and recognizes the worker unit; dashboard captions and one detail line;
  `manage.py provision_active_message_worker_check` (repair tool).
* **Migration `monitoring.0014_provision_active_message_worker_check`**: one
  `RunPython` (manual for Update Center; needs the r0116 companion bound to
  SHA-256 `e87ae5de…a245566`), no schema change, reverse a no-op. It creates
  the worker's check under the first free name of a bounded series, or fails
  explicitly -- an unattended install never succeeds without the check.
* No model, setting, capability or URL; no engine, renderer,
  OGRemote, Weather/AMBER or GStreamer change; `NATIVE_AIRING_AVAILABLE`
  stays False.

## Release notes for D3E-C

* New `active_messages/retention.py` (eligibility authority, retirement,
  safe sweeps) and `active_messages/reconcile.py` (read-only reconciler,
  deduplicated Monitoring attention); new `manage.py active_message_retention`.
* No migration, model, setting, capability, URL or timer. The retirement
  marker is the append-only `publication_retired` event.
* Nothing is reclaimed without `--apply`; nothing is selected by age;
  publication retirement is explicit, per execution, a trusted maintenance
  action recorded as the system (no asserted operator identity).
* Review corrections: the publication destination-lock sidecar is persistent
  (never unlinked); a Track may own a reserved path only under its
  destination lock (`active_messages.track_guard`), and cleanup never removes
  bytes any Track owns or that are not exactly the publication's; the default
  report writes nothing (`--attention` opts in).
* Final correction: nothing is deleted through a shared pathname any more.
  Every removal (publication audio, orphan, waveform) moves the name into a
  private quarantine with `renameat2(RENAME_NOREPLACE)`, proves the MOVED
  inode is the verified one, and only then removes it (zero-link proof);
  a replacement is put back without overwriting, or kept for manual recovery.
  Interrupted operations are settled by `--sweep --apply`
  (`retention.recover_quarantine`) and reported read-only (`quarantine_*`).
* Quarantine ownership correction: Track ownership covers BOTH `filepath` and
  `waveform_path`, matched by the object's device/inode (any alias) and by
  resolved name, through one bounded scan of every Track; unresolvable paths
  and database errors refuse. The Track write guard refuses any new reference
  resolving into a quarantine and requires the destination lock for claims of
  a reserved file or of another Track's publication waveform name, on every
  ORM path. No migration; no library schema change.
* Final guard authorization correction: protected path fields accept only
  literal values (`Value("...")` is judged as its string; every other ORM
  expression is refused); a Track id equal to a publication's
  `track_id_snapshot` authorizes nothing -- only the live publication's own
  Track (its foreign key) may update its own file names without the lock, as
  an update that cannot re-insert a row (`library.models.UPDATE_ONLY`); a lock
  holder re-validates that the file exists before referring to it.
* No engine, renderer, publication, OGRemote, Weather/AMBER or GStreamer
  change; `NATIVE_AIRING_AVAILABLE` stays False.

## Release notes for D3E-D

* Operator Board diagnostics (`/iportal/messages/board/diagnostics/`, and
  `.../diagnostics/data/` as JSON): D3E-C's reconciliation on explicit
  request, bounded (100 rows per class, metadata only), classified for an
  operator, never cached and never shown as health when it did not run;
  worker health exactly as Monitoring last validated it; read-only and
  CONTROL only. No destructive endpoint, no maintenance from the web.
* Airing detail: publication, retirement (`publication_retired`, SYSTEM via
  maintenance_command), insertion and play evidence, durable after
  retirement.
* Live status: no 90-refresh cutoff, one outstanding request, back-off, a
  stale view never asserts ON AIR, hidden-tab / back-forward / session-expiry
  handling, the worker chip.
* Corrective pass: an untrusted Monitoring document (future-dated, invalid
  or missing timestamp, malformed `checks` or entry, unreadable) is
  "Monitoring status unavailable", never a current healthy verdict and never
  an exception; the JSON diagnostics route inspects only on the same explicit
  `?inspect=1` as the page (`board.inspection_requested`).
* Nested-schema correction: the worker entry is validated against its
  producer's serialized contract, state by state (`field` names what is
  missing or wrong); an incomplete entry is "unavailable", never ok, verified
  or current, and a heartbeat is "current" only for conditions that passed
  the heartbeat's staleness bound.
* Producer-contract consistency: a bound heartbeat must be one observation
  the producer could write (code/execution formats, the writer's first-cycle
  rules, `heartbeat.assess`'s reason for exactly these fields, checked
  against the producer over a grid of every valid document); a first-cycle
  render with no outcome yet is a healthy current render.
* Crash window: `crash_looping` only at a serialized heartbeat age within the
  producer's 900 s crash window (900.0 possible, 900.1 and above never), and
  `history_unverified` never for a `starting` record from the future; the
  producer grid now spans every time bound on both sides.
* Reconciler: one shared Track-ownership scan per inspection
  (`retention.references_by_location`); `Report.generated_at`;
  `reconcile.inspect_publication`. Cleanup authorities unchanged.
* No migration, model, setting, capability, timer or scheduler;
  `NATIVE_AIRING_AVAILABLE` stays False; no engine, renderer, publication,
  OGRemote, Weather/AMBER or GStreamer change.
