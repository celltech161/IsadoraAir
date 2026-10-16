# Produced content — regular and TEST / NO AIR (iPortal Phase D3C)

D3C makes a station's **regular** produced-content types (news items,
calendars, PSAs, the USD 239 minute, 3 minutes to center, …) producible from
the iPortal, and gives producers a **TEST / NO AIR** practice workspace.
Urgent and public-address types stay with active messages
(docs/ACTIVE_MESSAGES.md, "My Messages").

Native active-message airing stays closed (`NATIVE_AIRING_AVAILABLE = False`)
and legacy OGRemote stays authoritative until the later cutover. Nothing in
D3C changes the engine, GStreamer, the renderer protocol, any systemd unit or
OGRemote. D3C is dormant until a station configures types and assignments.

## The architecture in one line each

```text
REGULAR  ProducedContentType authority
         -> explicit managed SpokenContentType backend
         -> the existing Phase-C spoken lifecycle / render / projection
         -> an ordinary Ready-to-Air library Track in the target category

TEST     validated temporary ProductionMedia only
         -> never spoken
         -> never library
         -> never air
```

## Regular content reuses Phase C (it is not reimplemented)

Phase C (docs/SPOKEN_CONTENT.md) already provides immutable submissions,
validation, the isolated `isadoraair-render` service, the **five code-backed
recipes**, the `SpokenItem` lifecycle and history, the leased
`isadoraair-spoken-render-worker`, current-edition and accumulating delivery,
atomic library projection, recall/expiry, reconciliation and deck-open
safety. D3C adds no renderer, worker, job table, service, projection system or
processor framework. The path is:

```text
ProducedContentType + assignment
  -> My Content: New submission        produced_content.regular.start
       -> backend.ensure_regular_backend      (re-proves the managed backend)
       -> spoken.transitions.create_item      (one DRAFT SpokenItem; the producer submits it)
  -> shared recorder: Submit for processing   produced_content.regular.submit
       -> spoken.transitions.submit + record_validation   (one transaction)
  -> VALIDATED -> the existing leased worker -> PROCESSING -> isadoraair-render (the accepted recipe)
  -> PROCESSED -> (requires_review = False) -> spoken.delivery -> DELIVERED
  -> ordinary library Track (FLAC, Ready-to-Air) -> ordinary rotation / log / playout
```

Nothing renders or projects in the web request. There is no priority
insertion, special lane or engine command: delivered material is an ordinary
Track. The five recipes are exactly `cal_events`, `usd239min`, `news_item`,
`3min_ctr`, `lcl_psa` — Phase C's versions 1, unchanged (beds by the logical
`ReferenceAudio` keys `bed.cal_events`, `bed.usd239min`, `bed.news_intro`,
`bed.3min_ctr`; `lcl_psa` has none). A missing or unusable bed fails the item
through the existing render/reference authority (`reference_missing`, …). D3C
imports no KOGR bed files and seeds no station types.

### The managed backend (`produced_content/backend.py`)

`ProducedContentType.spoken_content_type` (nullable OneToOne, PROTECT) is the
**explicit** identity of a regular type's backing `spoken.SpokenContentType`.
Ownership is never inferred from names (`key == code`): a standalone Phase-C
type with a matching code is never adopted, and its items are not
produced-content items. Non-regular types never have a backend (refused by
`save()`/`clean()`; a backed type's delivery mode is fixed).

**One source of configuration truth.** The ProducedContentType is the
station-facing authority; the backing row is its execution representation.
`ensure_regular_backend(content_type)` is the only writer of the link: it locks
the exact produced type, requires it regular and runnable, creates the backing
row (generated durable code `produced-<uuid hex>` — never the producer key) or
re-synchronizes it, and returns it. Every regular producer operation (start,
submit) goes through it, so production always runs on a re-proven
configuration; concurrent first synchronizations create exactly one backend
(the produced type's row lock).

**The link is bridge-only identity**, enforced at the ORM boundary and in the
database:

* an ordinary `ProducedContentType.save()` never writes the link (a stale
  admin form cannot erase it) and refuses an instance whose link was attached,
  replaced or cleared in memory; `save(update_fields=[...link...])` is refused;
* the model's queryset -- its default AND base manager (`_base_manager`, set
  on `_meta` as spoken does, no migration) -- refuses `update()` /
  `bulk_update()` / `bulk_create()` of the link, except the bridge's single
  granted FIRST link of one exact, still-unlinked regular row;
* `produced_type_backend_regular_only` (CHECK, `produced_content.0004`): a
  backend implies a regular type; a backed type's mode is fixed.

**Authorization precedes every side effect.** The bridge itself authorizes
nothing; `produced_content.regular` first locks and re-reads the exact type
and proves it enabled and the user's (`authority.lock_for_production`:
CONTROL, or PRODUCE plus an assignment, whose row is locked too) -- and for a
submission that the item is the user's own -- and only then calls the bridge.
A refused start or submit creates, links or synchronizes no backend and
creates no item.

| ProducedContentType | backing SpokenContentType |
| --- | --- |
| `enabled`, `name`, `description` | same |
| `output_mode = single` | `delivery_mode = current_edition` |
| `output_mode = accumulating` | `delivery_mode = accumulating` |
| `recipe_key` | the same logical recipe key (one of the five) |
| `target_category` | the same exact Category |
| `artist_tag` | `artist_label` (blank: the type's name) |
| — | `requires_review = False`, `deliver_ready_to_air = True`, `on_current_recall = retire_asset`, `recall_policy = owner_anytime`, `title_policy = item_title`, `default_lifetime = None`, `recipe_params = {}` (the recipe's own defaults) |

A regular type with a blank or unknown recipe is **not runnable**: production
fails closed with a configuration error and no backend is created (an existing
one is disabled when the station saves the type). Nothing executes a path,
command, import or script — the recipe key is a lookup in a fixed set. (D3A's
recipe-key pattern refused a leading digit and so the accepted `3min_ctr`;
`produced_content.0003` widens it.)

**No drift.** Django Admin shows a linked SpokenContentType **read-only and
undeletable** ("Managed by Produced Content"); the Produced Content admin shows
the backend link and its health and synchronizes on save. Unlinked
SpokenContentTypes remain ordinary Phase-C configuration. Phase C's own
validation and locking inside `SpokenContentType.save()` are unchanged.

Lock order: `ProducedContentType (-> its assignment) -> SpokenContentType ->
SpokenItem -> ProductionMedia` (the produced type ahead of Phase C's own
order). `ProducedContentType.save()` takes the same type-row lock first, so a
mode change is serialized against a first backend link or first test take.

### Delivery semantics (exactly Phase C's)

* **single** → one stable `SpokenAsset` and one Track; each successful delivery
  replaces the current edition atomically (the previous item becomes
  SUPERSEDED); an old edition never returns automatically. Starting or
  recording a new submission never removes the current edition.
* **accumulating** → one asset/Track per delivered item; earlier items stay
  until recalled.
* Track: category = the type's target category, `ready2air = True`, FLAC,
  artist = `artist_tag` or the type name, title = the item's title (else the
  submission date). No MP3 parity output, no Local PSA top-of-hour logic.

### Recall, withdrawal, abandonment

The owner -- or station control (below) -- withdraws one exact item through
the existing spoken lifecycle
(`spoken.transitions.recall`, which hands delivered/superseded items to
`spoken.delivery.recall_delivered`): a DRAFT is abandoned, a submitted /
validated / processed / failed item is withdrawn, a delivered item is recalled
(single: the Track becomes unavailable and its bytes are removed; nothing is
restored; a stale recall of a superseded edition never touches the newer one;
accumulating: only that item's projection). A PROCESSING item cannot be
interrupted (the lifecycle has no such transition). History is never deleted.
Losing an assignment, or a disabled type, refuses only NEW production — never
an owner's withdrawal of their own material.

## Producer authority

D3C reuses D3A's authority — no new capability, migration or permission
system. The established `active_message.produce` / `active_message.control`
capabilities are the broad Produced Content producer / control capabilities:

* a producer needs PRODUCE **and** an assignment to the exact enabled type
  (assignment is scope, not role);
* CONTROL produces any enabled regular/test type without an assignment.

**Station control** (CONTROL, or a spoken manager/staff) manages station
Regular material across producers: My Content lists every producer's items of
the managed types for it, read-only in the recorder (only an item's own
producer records or submits it), and it can abandon / withdraw / recall any
of them wherever the spoken lifecycle permits (PROCESSING stays
non-interruptible; a stale recall of a superseded edition never touches the
newer one).

A withdrawal carries the state its confirmation showed (`expected_state`).
The page's early comparison is presentation only; the AUTHORITATIVE check is
inside spoken under the lifecycle's own locks (`transitions.recall(...,
expected_state=)`: on the locked item row for pre-delivery recall, and inside
`spoken.delivery` after its advisory -> type -> item locks for delivered /
superseded recall). A stale confirmation -- a draft submitted meanwhile, a
delivered item superseded meanwhile -- is refused with no change.
`produced_content` never locks the item itself before entering projection
recall (that would invert the projection lock order). The spoken domain was adapted narrowly for this: a registered
recall authority (`spoken.transitions.register_recall_authority` /
`may_recall_as_manager`, consulted only by recall, under the item's row lock)
through which `produced_content` names CONTROL as manager of exactly the
spoken types explicitly linked by `ProducedContentType.spoken_content_type`.
Standalone, unlinked Phase-C types gain nothing (their manager is still only
staff); retry, review and reattach are unchanged. An ordinary producer sees and
withdraws only their own items (another producer's is a 404).

## The workspace: My Content (`/iportal/content/`)

Separate from My Messages (different lifecycles). Listed in the `/iportal/`
hub only for someone with at least one assigned enabled regular/test type, or
CONTROL. Per regular type: name, target category, single/accumulating,
recipe, backend health, the current edition (single), the producer's recent
items with their exact Phase-C state (Draft, Submitted, Validated, Processing,
Processed, Delivered, Superseded, Recalled, Invalid, Failed), New submission,
Continue / Open, and Abandon / Withdraw / Recall (with a confirmation that
carries the expected state). The producer's unfinished items of types no
longer offered stay visible so they can be withdrawn. Every change is a
CSRF-protected POST naming one exact type key or item UUID, re-authorized by
`produced_content.regular`.

## The recorder adapter (`produced-content`)

One fixed registered adapter (`produced_content/iportal.py`), mounted under
`/iportal/content/record/`. Subjects are server-resolved, one exact object:

* `?item=<uuid>` — REGULAR: one SpokenItem of a managed backend. Only its own
  producer sees it (anyone else: 404). A DRAFT is writable while its producer
  may still produce the exact enabled, runnable type. Its "save" is the
  explicit submission — **Submit for processing** ("browser only; nothing is
  submitted or on air until you submit"): re-authorize, re-prove the backend,
  lock the item, compare the opaque revision (state + submission + latest
  event) **under the row lock**, require a VALID take of the producer's own,
  then `submit` + `record_validation` in one transaction → VALIDATED (or
  INVALID by the type's duration policy). A stale second tab gets **409**.
  Submitted items are read-only previews. There is no second persisted draft
  audio: the browser-local draft is the editable draft.
* `?test_type=<key>` — TEST: the signed-in user's own binding for one exact
  test type, resolved through the recorder core's `resolve_for(user, params)`
  hook (the identity always comes from the session).

Order for every operation: broad capability → exact subject → exact
type/assignment/ownership (`authorize_subject`) — before any intake, preview,
source or commit work.

## TEST / NO AIR (`produced_content/no_air.py`)

`ProducedContentTestTake` — `(user, exact test type) → one validated
NO-AIR ProductionMedia` (unique per user and type; media PROTECT while bound;
opaque revision; `expires_at`). Temporary media state, not broadcast history; no
event subsystem.

A save locks in the order `ProducedContentType -> ProducedContentTestTake ->
ProductionMedia`: the exact type is locked, re-read and proven still TEST,
enabled and the user's BEFORE the binding is locked or created, so a binding
always names a type that is TEST at the commit point (a concurrent mode change
either commits first -- the save is refused -- or waits and is then refused
itself because test takes exist).

A producer may record, import, edit, validate, save, preview, replace and
remove their test take. The recorder says **TEST / NO AIR**, **Save test
take**, *Nothing in this workspace can air.* Saving binds under
`retention.lock_for_no_air_binding` -- which accepts only NO-AIR media, so an
ordinary take is refused (`not_test_media`) -- and compares the revision under
the binding's row lock (a stale tab gets 409, never a silent overwrite).

TEST media cannot reach air through any application path (the services and
the guarded ORM; raw SQL and Django's plain `models.QuerySet` stay outside
that boundary, as for every ProductionMedia invariant), at two levels:

* the TEST code: no TEST type has a spoken backend, and the TEST code calls no
  spoken transition, renderer or projection and creates no SpokenItem,
  rendition, SpokenAsset, Track, LogItem, active message, execution, queue
  entry or engine command (asserted directly by the tests);
* the media itself (r0113 correction -- the D3C candidate let a saved test
  take be committed to a REGULAR draft and delivered as a Ready-to-Air Track):
  every take the TEST recorder makes -- record, import or edit, saved or never
  saved -- is NO-AIR `ProductionMedia` **from intake on**
  (`airing_prohibited`, stamped from the adapter's
  `media_airing_prohibited(subject)`, never from the request). It is immutable
  and every descendant inherits it -- an edit of a TEST take made in the
  regular, VoiceTrack or active-message workspace is NO-AIR too. The canonical
  air-capable binding (`retention.lock_for_binding`) refuses it, so regular
  submission (HTTP, `regular.submit`, `spoken.transitions.submit` -- as owner
  or manager), VoiceTrack, active-message media, aggregates and reference
  audio all refuse it (`airing_prohibited`). Replacing, discarding or expiring
  the TEST binding does **not** remove the restriction, even when another
  reference keeps the bytes alive. See docs/PRODUCTION_MEDIA.md.

Retention: **7 days after the most recent save** (fixed; not configurable in
D3C). When a binding is replaced, removed or expires, only the binding is
deleted (through `no_air`), and after commit the previously bound media gets
an attempt at the ordinary safe purge (`production.services.retention.
purge_media`) — if anything else references it (e.g. an edit derived from it),
the purge refuses harmlessly and ordinary retention decides. The bounded
expiry sweep (`no_air.expire_due`, 100 per pass) runs from the existing
`spoken_render_worker` loop at most once a minute, isolated: a failure is
logged and never stops rendering. No new service, unit or timer.

## Migrations

| Migration | Operation | Classifier |
| --- | --- | --- |
| `produced_content.0002_d3c_spoken_backend_and_test_take` | AddField `spoken_content_type` (nullable OneToOne) | additive |
| | CreateModel `ProducedContentTestTake` (unique (user, type), revision ≥ 1 and the expiry index folded in) | additive |
| `produced_content.0003_recipe_key_leading_digit` | RemoveConstraint `produced_type_recipe_key_shape` | manual |
| | AlterField `recipe_key` (validator `^[a-z0-9][a-z0-9_]{0,63}$`) | manual |
| | AddConstraint `produced_type_recipe_key_shape` (the widened pattern) | manual |
| `produced_content.0004_backend_regular_only` | AddConstraint `produced_type_backend_regular_only` (a backend implies a regular type) | manual |
| `production.0003_media_airing_prohibited` (r0113 correction) | AddField `ProductionMedia.airing_prohibited` (non-null boolean, Python default and PostgreSQL `DEFAULT false` kept: existing rows, and rows an older still-running writer inserts before the restart, are air-eligible) | manual (database default) |

No data migration, no station seed, no authz migration; existing
compatibility types stay valid (no backend required). 0003 only widens the
pattern (every existing row satisfies it); 0004 only guards what the bridge
already guarantees (every existing row satisfies it). Their four manual
operations need a release migration authorization when D3C ships, and
`production.0003` adds a fifth: its database default is deliberate (the
updater migrates before it restarts, and r0112 processes insert
ProductionMedia without the new column -- a dropped default would make those
inserts fail), and the classifier reviews any database default.

## Not in D3C

The operator Active Messages board and Replay Now (D3D), monitoring/history
(D3E), Active Message worker packaging, cutover / OGRemote retirement, legacy
import (categories, assignments, users, submissions, files), KOGR assignment
migration or station seed data, new GStreamer paths, playback lanes, engine
commands, renderer service or units, an approval workflow, Local PSA
top-of-hour alternation, arbitrary recipe commands/paths, MP3 parity output.
