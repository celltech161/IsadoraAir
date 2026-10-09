# iPortal Phase C — spoken content (2.22C)

Spoken/produced content: news, underwriting/ministry reads, calendar and
community announcements, PSAs, 3-minute-center material and similar ordinary
produced spoken audio. Architecture B′ (accepted 2026-10-03): the shared
`production` app is the media substrate (immutable `ProductionMedia`, intake,
validation, preview, retention, **and the render engine**); the `spoken` app
is one independent domain above it with its own identity, lifecycle, review,
recall, expiry and library-projection policy. There is no `ProductionJob`, no
generic workflow table, no generic foreign key and no workflow state on
`ProductionMedia`. Foreign keys point domain → `ProductionMedia` (PROTECT),
never the reverse.

**Phase C1 (this slice)** delivers the permanent schema, the lifecycle
authority, the render engine, the five recipes and reference audio. It does
**not** create, change or delete library Tracks or files, run expiry, or
enable any production type — Track projection is C2.

## Schema (`spoken.0001_initial`, `production.0002_referenceaudio`)

Both migrations are pure `CreateModel` operations — every constraint and
index folded in, models created in dependency order so no foreign key needs a
later `AddField` — and classify **additive with zero manual operations** under
the protected updater's own probe (`spoken/tests/test_migration.py`: static and
live). No `RunPython`, no data, no seeded types or capabilities.

| Model | Purpose | Permanent constraints |
| --- | --- | --- |
| `SpokenContentType` | station-configured type: `code` (slug, permanent once used), text, `enabled` (default off), `delivery_mode` (`current_edition` / `accumulating` / `retain_only`), `recipe_key` + bounded `recipe_params`, media limits, `target_category` (PROTECT, nullable), `artist_label`, fixed-enum `title_policy`, `requires_review`, `deliver_ready_to_air`, `on_current_recall`, `recall_policy`, `default_lifetime`, `media_retention_days` | code shape; recipe required; limits positive and ordered |
| `SpokenItem` | the authoritative workflow object (UUID): type (PROTECT), `origin` (`submitted` / `assigned` / `imported`), submitter (SET_NULL) + username snapshot, title/notes, `submission_media` and `rendition_media` (PROTECT), `state`, every lifecycle timestamp, failure code/detail, review and recall fields, worker lease (`processing_attempts`, `lease_token`, `lease_expires_at`), dormant import provenance | lease token/expiry both-or-neither; **partial unique (`import_source`, `import_external_id`) where `import_source` ≠ ''**; import provenance all-or-nothing and only with `origin=imported`; attempts bounded; indexes (state, lease), (type, state), (submitter, submitted) |
| `SpokenItemEvent` | **the** authoritative history: item, event slug, from/to state, actor kind + user (SET_NULL) + username snapshot, optional media / asset / edition (PROTECT), bounded JSON detail, time | append-only (see guards); index (item, time) |
| `SpokenAsset` | stable airable identity: type, `mode`, `slot_key`, accumulating `item`, `track` (1:1 `library.Track`, SET_NULL), deterministic `library_relpath` = `<category>/spoken-<uuid>.flac` (unique), `current_item`, `current_rendition`, `state`, `generation`, retired time/reason | **one non-retired current-edition asset per (type, slot_key)**; **one non-retired accumulating asset per item**; item ⇔ accumulating; retired ⇔ retired_at; slot-key and relpath shapes (no `..`, no leading `/`) |
| `SpokenAssetEdition` | edition/promotion history: asset, item, `rendition` (PROTECT), generation, state (`pending` / `active` / `superseded` / `aborted`), library SHA-256, promoted / superseded times, `superseded_by` | **one `active` edition per asset**; unique (asset, generation); SHA-256 shape; active ⇒ promoted; superseded ⇒ dated |
| `production.ReferenceAudio` | domain-neutral key → pinned retained media (beds) | key shape; SHA-256 shape; **one current row per key** |

Deliberately **not** constrained in the database: the set of item states (a
state-set check would turn any future state into a manual `AlterConstraint`;
the transition table enforces it), and the platform byte/duration ceilings
(they may change; the service enforces them on every save). Phase E assignment
fields (assignee, due date) are deferred: they are nullable `AddField`s, which
classify additive later.

## Lifecycle authority (`spoken/transitions.py`)

Every state change goes through one service: lock the item with
`select_for_update(of=("self",))`, check the fixed transition table, check
actor/policy/evidence against the locked row, bind any media under
`production.services.retention.lock_for_binding` (the binding rule), update
state and timestamps, append the `SpokenItemEvent` — all in one transaction.

| Transition | Who | Evidence / policy |
| --- | --- | --- |
| (none) → draft | user | type enabled; `imported` origin reserved for the Phase F import tooling |
| draft → submitted | submitter or manager | type still enabled; a recording/upload/edit, within the type's byte cap; bound under the binding rule |
| submitted → validated / invalid | system | the submission's own validation verdict and the type's duration bounds |
| validated → processing | worker | a fresh lease (30 s – 1 h); attempts counted |
| processing → processing | worker | reclaim only after the lease **expired** (new token) |
| processing → processed | worker | the current lease token; `RenderEvidence` whose rendition is EXACTLY the render the type authorizes now (see *Render completion*); bound |
| processing → failed | worker | the current lease token; failure code/detail |
| failed → validated | manager | retry |
| processed → awaiting_review | system | only if the type requires review |
| awaiting_review → rejected | manager | review note |
| pre-delivery → recalled | manager, or the submitter while `recall_policy` allows | not while `processing` |
| → delivered, delivered → superseded / recalled / expired, superseded → delivered | — | **in the table, refused until C2** (`ProjectionRequired`) |

"Manager" in C1 is staff/superuser; per-type grants are Phase E.

**Authority boundary** (`spoken/guard.py`, the guarded querysets in
`spoken/models.py`). Ordinary Django ORM code can read spoken state but not
fabricate, mutate or destroy it outside the services. The guarded querysets
are the **default and the base** managers (`Meta.base_manager_name`), and
every reverse related manager (`content_type.items`, `item.events`,
`user.spoken_items`, …) is built from them, so each API below behaves the
same through all of them (proven per manager in `spoken/tests/test_authority.py`):

| API | `SpokenItem` | `SpokenItemEvent` | `SpokenContentType` |
| --- | --- | --- | --- |
| `save()` adding / `create()` / creating branch of `get_or_create()` / `update_or_create()` | refused unless it is exactly the row an open `ItemCreate` grant describes | refused unless it is exactly the event an open `EventCreate` grant describes | allowed — through the validated, row-locked `save()` |
| `bulk_create()` (plain, `ignore_conflicts`, `update_conflicts`) | **always refused** | **always refused** | **always refused** |
| `save()` of an existing row / updating branch of `update_or_create()` | only title/notes change — unless it is exactly the write an open `ItemWrite` grant for this row describes | refused (append-only) | validated, row-locked `save()` |
| `QuerySet.update()` | title/notes; a user FK to NULL alone (Django's SET_NULL) — never a protected column, grant or not | only `actor=None` (Django's SET_NULL) — every other column refused | **always refused** |
| `bulk_update()` | title/notes only, grant or not | **always refused** | **always refused** |
| `delete()` (instance, QuerySet, any manager) | **always refused** | **always refused** | refused while items exist (row-locked; PROTECT also refuses) |

A `state` that is not an `ItemState` is refused on every write path, grant
or not. The state set is enforced by this boundary, deliberately not
by a database check constraint (a new state must never need a manual
migration). No foreign key into an item or an event cascades, so Django's
deletion collector never reaches them. Admin is read-only for items, events,
assets and editions. Raw SQL is outside the boundary, as for ProductionMedia.

**Operation-specific write authority** (`spoken/guard.py`,
`spoken/lifecycle.py`). There is no trusted "inside the service" context.
The transition service validates a transition and computes ONE **deeply
immutable** `TransitionPlan`: tuples of `(attname, value)` pairs holding only
immutable scalars, with event `detail` stored as canonical JSON text (the
constructor refuses anything mutable, so neither the caller's input nor a
holder of the plan can change it after validation). Two grants derive from
that plan, each opened only around one call to `guard._execute()`:

* `ItemWrite(item id, from state, to state, {attname: exact value})` /
  `ItemCreate(item id, values)` — one item write or creation;
* `EventCreate({every event column: exact value})` — one event insert.

`_execute()` takes **no data**: it writes exactly the open grant's payload with
one ORM statement that exposes no model instance to application code and
emits **no model signals** (base `QuerySet.update` filtered by id AND source
state; base `QuerySet.bulk_create` of an instance built from the payload —
Django still applies `auto_now_add`, primary keys and JSON encoding). It then
reads back the **complete authoritative row** — the canonical column set from
`guard.authoritative_item_fields()` (every SpokenItem column except the
primary key and title/notes; the same definition the ordinary-save guard
uses), never just the grant's own columns — and compares every column with
the grant's sealed expectation:

* an existing item: the snapshot taken right after the row was locked (before
  any callback runs; never a later re-read) with the plan's values applied;
* a new item: the plan's values and the model default for every other column;
* an event: every column of its payload.

Generated columns are checked for their approved semantics instead of a value
nobody knew in advance: the primary key exists and `created_at` was stamped
during the write. Any difference — a planned value, an unplanned column such
as `failure_code`, or an event column — raises and rolls back the whole
transition (item and event together; a normal retry builds a fresh plan).
`pre_save` receivers, descriptors or anything else between authorization and
SQL therefore cannot change what is written. `Model.save()` never honors a
grant. Title/notes are outside the lifecycle expectation: the transition's
SQL never writes them, and because the transition holds the item's row lock,
a concurrent ordinary edit waits and then lands on top of the committed
transition (two-session test in `spoken/tests/test_locking.py`).

**Event detail is strict canonical JSON** (`guard.canonical_json`): objects
with string keys, arrays (tuples become arrays), strings, finite numbers,
booleans and null; sorted keys, compact separators, ASCII. Decimal, UUID,
datetime/date, sets, bytes, arbitrary objects, non-string keys, NaN and
±Infinity, and nesting deeper than 32 are refused at planning
(`InvalidEventDetail`) — before any plan, grant or write; nothing is ever
stringified, so no runtime representation can enter the evidence. Callers
pass such values as explicit strings when they need them.

A grant is itself checked against the fixed transition table: it can never
describe a C2 (library) transition, a column its rule does not write, a state
other than its target, or an event its rule does not record. A grant is
consumed by its execution, cannot be nested, is bound to the thread and
asyncio task that opened it, and is always removed on exit. Only plans built
by the validation path are committed, once. Completion opens no grant until
locks, lease, persisted rendition evidence and expected provenance have all
been verified.

Ordinary saves of an item (title/notes) still run normally and emit signals,
but **write only the editorial columns** (`update_fields` is narrowed), so a
`pre_save` receiver cannot smuggle a protected value into that SQL; a
`post_save` receiver attempting a lifecycle write is refused like any other
ORM caller. The same post-signal rule covers the other entry-checked writes:
`SpokenContentType.save()` re-reads and re-validates the row it wrote (and
re-checks code permanence) before its transaction can commit, and a
`ReferenceAudio` insert is compared with its exact registration before the
registering transaction can commit.

The transition functions are the supported mutation API. Grants, plans,
`_authorized` and `_execute` are private implementation details of `spoken`;
deliberately importing them (or calling Django's base `QuerySet` methods
directly) is equivalent to raw SQL and outside the ORM threat model.
(`spoken/tests/test_transition_authority.py`.)

**Content-type configuration** is written only through
`SpokenContentType.save()`, which in ONE transaction locks the persisted row
(`SELECT … FOR UPDATE`), checks code permanence against the items that exist
while the lock is held, validates (code shape, recipe and parameter schema,
media limits, enabled-projection category) and writes. `create_item()` locks
the same row before creating an item, so a rename and a first item are
serialized: a rename that locks first commits and the item is created against
the renamed type; an item that locks first commits and the rename then sees it
and is refused. (`spoken/tests/test_config_race.py`, two real sessions.)

## Render engine (`production/services/render/`)

Domain-neutral. A **recipe** is a stable `key`, an integer `version` (bumped
whenever output semantics change), a typed/bounded parameter schema, and a
fixed composition of the **step library**: `Highpass`, `TrimSilence`,
`BedUnderlay` (`loop_fade` / `fade_if_longer` / `natural_end`), `DelayVoice`,
`Fade`, `Loudnorm` (two-pass, `linear=true`), `EncodeFlac`. Steps hold only
range-checked numbers and emit filter text with fixed formats; configuration
can never supply a filter, command or path. A reference parameter is a slug
resolved through `ReferenceAudio`.

`render(source, recipe_key, params)` never renders in the calling process.
Since Phase C2A it is the client of a **separate, sandboxed render executor**
(`isadoraair-render`, below):

1. `render/client.py`: recipe from the code registry, parameters validated
   (unknown, wrong type, out of bounds, non-finite, path-like or
   expression-like values refused); the source must be present and valid;
   beds resolved by key and hash-verified; source and beds are opened
   **read-only** and sent as descriptors with the canonical provenance digest
   the client expects;
2. the executor proves every descriptor's bytes, the recipe version, the
   parameters, the pinned references and the digest, runs the fixed steps —
   prepare (48 kHz stereo), mix, loudness analysis (JSON parsed structurally;
   `-inf` ⇒ `silent_render`), apply + FLAC — and returns the FLAC as a
   **sealed memfd**;
3. `render/rendition.py` — the **sole rendition-creation authority** —
   streams that same descriptor through the ordinary derivative intake:
   validated by the validation service (ffprobe, full decode **and the
   engine's GStreamer decode**) and recorded with `derived_from`, recipe
   key/version, the canonical provenance digest (`render/provenance.py`) and
   the executor's toolchain identity (`…; executor isadoraair-render/1`).

Failures raise `RenderError` with a stable code (and `retryable`) and leave
**no bindable rendition**: an invalid rendition is refused by intake (no row);
a validation-infrastructure failure leaves an `unvalidated` row nothing can
bind; the source is never touched; retries make new renditions. A render that
completion later finds outdated stays an immutable, unbindable orphan for
ordinary retention.

### Render completion (`transitions.complete_processing`)

`render()` returns a `RenderResult`; its `.evidence` is a frozen
`RenderEvidence` of primitives (rendition id, source id, recipe key, recipe
version, provenance digest) — the shape C2 will carry over a socket. It is a
**claim, not authority**: anyone can construct one. Completion locks, in
order, the content type (the row configuration saves lock), the item, the
rendition (`lock_for_binding`) and each reference key (shared advisory lock),
then requires:

* the caller holds the current lease;
* the rendition is a valid `rendition` derived from this item's submission;
* its **stored** recipe key, version and provenance digest equal the
  evidence's (a forged or swapped claim → `rendition_mismatch`);
* and equal what `render.expected()` — the same helper `render()` uses —
  computes from the type's **current** recipe, parameters and **current**
  reference registrations (another version, other parameters or a replaced
  bed → `render_outdated`);
* no other item has bound it (`rendition_already_bound`).

If the configuration or a bed changed while the render ran, completion is
refused and nothing is written; the worker renders again (or fails the
attempt). Because configuration saves and bed registrations take the locks
completion holds, the only orders are "completion under the old
configuration, then the change" or "change, then the old render refused".
No in-flight configuration snapshot (and so no migration) is needed in C1.

### The render executor — `isadoraair-render` (Phase C2A)

A **separate** service from `isadoraair-validation`
(`deploy/isadoraair-render.service`; `manage.py production_render_service`;
`production/services/render_service.py`): its own unit, Unix socket
(`/run/isadoraair-render/render.sock`, 0600 in a 0700 runtime directory,
same-account peers only via `SO_PEERCRED`), delegated cgroup subtree
(`<unit>/iportal-render/run-<id>`), admission pool, limits, status file and
Monitoring card. A render — two ffmpeg passes over a whole programme — can
therefore never consume validation capacity, delay uploads, or change the
validator's authority; each degrades independently (tested both ways).

**Protocol** (`render/protocol.py`, version 1): one connection, one request,
one answer, each a JSON line. The only operation is `render`: a
code-registered recipe key and version, the type's raw parameters, the source
and each pinned reference as `{media, sha256, bytes, duration}` with their
**read-only descriptors** (SCM_RIGHTS), the expected provenance digest, a
timeout and a correlation (`item`, `lease`). Nothing in it can name a
command, executable, ffmpeg option, filter, path or URL. The answer is
`status: ok` with exactly one **sealed memfd** (`F_SEAL_WRITE|GROW|SHRINK|SEAL`)
and its size and SHA-256, the recipe, digest, provenance document, measured
loudness and the executor's toolchain — or a stable error code (never tool
output or paths; those go to the service journal, bounded).

**Executor** (`render/executor.py`): every received descriptor must be a
regular file opened **read-only** whose size and SHA-256 are the claimed ones
(a substituted descriptor is refused before anything runs); the recipe
version must be the registered one; the pinned references must be exactly the
ones the plan uses; its own canonical digest must equal the client's
expectation. Each step is a fixed ffmpeg argv built from the step library with
the pinned absolute ffmpeg, `-protocol_whitelist fd`, inputs as fresh
read-only reopens (`-fd N -i fd:`) and outputs as service-created memfds
(`-fd N fd:`) — no path appears anywhere.

**Sandbox** (every step, `confinement.execute` with the launcher's `render`
profile): cgroup leaf limits (1.5 GiB for the step's tree, 64 tasks, one CPU,
30 CPU-minutes), no_new_privs, **Landlock with no filesystem writes at all and
reads only beneath /usr, /lib, /lib64, the loader cache/timezone in /etc and
the CPU topology** (not the station's homes, source tree, media library,
configuration or keys), **seccomp denying `socket(2)`** (no network, netlink
or unix sockets) and `clone3`, rlimits (AS 2 GiB, FSIZE 1 GiB, NOFILE 256, no
core), a fixed environment (`PATH=/usr/bin:/bin`, `LC_ALL=C.UTF-8`,
`HOME`/`TMPDIR` nonexistent — nothing inherited), its own session, the
service's hard deadline (`120 s + 6 × duration`, ≤ 1 h), and `cgroup.kill` of
the whole tree when the step ends. Start-up destroys leftover leaves and runs
a confined toolchain probe before the socket is bound. The unit adds
`PrivateNetwork`, `RestrictAddressFamilies=AF_UNIX`, `ProtectSystem=strict`,
`ProtectHome=read-only`, no capabilities, `MemoryMax=3G`, `TasksMax=256` and
`CPUWeight=50` (a render yields to playout).

**Admission**: at most 1 render at once (operator domain 1–2) and 2 admitted
requests waiting (0–4); anything beyond is answered `busy` from the accept
loop without reading the request. A slot is released on success, failure,
timeout and client disconnect (closing the connection cancels the render);
a failed step or a malformed request never affects the next one.

**Sole rendition authority**: a `rendition` row can be inserted only inside
`production.models.rendition_authority`, which `render/rendition.py` opens
naming the exact row (parent, recipe key/version, digest, executor
toolchain, output SHA-256) for an `ExecutorOutput` the client issued (identity
sealed, single use). `ProductionMedia.save()` refuses any other rendition
insert — and re-reads the row afterwards, so a `pre_save` receiver cannot
alter it; `bulk_create` refuses renditions; generic intake refuses
`kind=rendition` before staging a byte. Completion additionally requires the
executor's identity in the rendition's toolchain. Raw SQL is outside the
boundary.

**Worker** (`spoken/worker.py`; `manage.py spoken_render_worker [--once]`;
`deploy/isadoraair-spoken-render-worker.service`): claims a validated item (or
one whose lease expired) with `start_processing` — the item lease is the only
authority, no job table — renders, completes with the exact evidence, or
fails. Transient failures (busy, service/sandbox unavailable, timeout, an
unvalidated rendition) retry twice within the lease (2 s, 10 s); one
`render_outdated` re-renders against the current configuration; deterministic
failures fail at once; more than 3 claims fail `attempts_exhausted`; a lease
lost to expiry is left to its new holder. Operator-attention conditions emit a
`spoken_render` SystemEvent; SpokenItemEvent stays the history. Nothing renders
in the web request path.

**Monitoring** (`production/services/render_health.py`, the Systemd Service
check's readiness hook for `isadoraair-render.service`): socket, status file
(limits, renders running/waiting, last failure code and time) and an
empty-request handshake — read-only; the card has no Restart button.

**Deployment** (not done in C2A): the two units are not in the protected
runtime's managed-unit policy, so Update Center cannot install or restart
them yet; a release must declare them and a new protected-runtime generation
must authorize them (or an operator installs them). No content type is
enabled.

## The five recipes (`spoken/recipes.py`, all version 1)

Ported from the proven legacy processors (`ogremote-ingest/processors/*.py`);
exact historical settings are the defaults. Common: 100 Hz high-pass, 48 kHz
stereo, two-pass loudness to −16 LUFS / −1 dBTP / LRA 5, FLAC 48 kHz (not the
legacy 320 kbps MP3).

| Key | Steps | Parameters (default, bounds) |
| --- | --- | --- |
| `cal_events` | silence removal (−35 dB, 0.5 s); bed **loops** under the voice and fades out after it; ends with the fade | `bed` (`bed.cal_events`), `bed_gain_db` (−13, −30…0), `fade_seconds` (5, 1…15) |
| `usd239min` | voice 10 ms after the bed; a bed longer than the voice fades 3 s after it, a shorter bed ends on its own (legacy code never loops it, despite its docstring) | `bed` (`bed.usd239min`), `bed_gain_db` (−13, −30…0) |
| `news_item` | silence removal (−36 dB, 0.5 s); intro bed from the start, voice enters later; both play out | `bed` (`bed.news_intro`), `bed_gain_db` (+6, −12…+12), `voice_delay_ms` (4000, 0…15000) |
| `3min_ctr` | bed plays to its natural end | `bed` (`bed.3min_ctr`), `bed_gain_db` (−10, −30…0) |
| `lcl_psa` | high-pass + loudness only | — |

As in legacy, a mono voice is upmixed with ffmpeg's −3 dB centre mix, so it
sits 3 dB lower against a stereo bed than its file level suggests.

## Reference audio (beds)

Nothing existing fit (`FXCart` is a mutable absolute path with no hash pin),
so `production` gains the smallest domain-neutral mapping: `ReferenceAudio`
(key → one current row → a `bed` ProductionMedia + the SHA-256 pinned at
registration). `resolve()` refuses a missing, retired, purged, not-valid or
hash-mismatched reference and hands the renderer an open descriptor of the
verified bytes. No production bed is ingested in C1.

**Authority.** `production.services.reference_audio` is the only writer:

* `register(key, media)` — under a transaction-scoped PostgreSQL advisory lock
  for the key (`pg_advisory_xact_lock(0x52454641, first 32 bits of
  SHA-256("isadoraair.production.reference_audio\0" + key))`; stable across
  processes; a collision only serializes two keys), it locks the current row,
  binds the media under `lock_for_binding`, requires a valid `bed` whose stored
  bytes re-hash to its SHA-256, retires the current row and inserts the new
  one — one transaction, so a failed insert leaves the former registration
  current. Re-registering the media already current is idempotent. Same-key
  races serialize on the advisory lock (including the very first registration,
  when there is no row to lock); the partial unique constraint stays as
  defense in depth and is never the normal race outcome.
* `retire(key)` — the one mutation: a state-qualified UPDATE of exactly the
  retirement columns of the current row; idempotent (`None` when nothing is
  current).
* Everything else is refused through the default, base and reverse managers:
  creation outside the registration scope (which authorizes only the exact
  key/media/SHA being inserted), any `save()` of an existing row, any
  `update()` except Django's SET_NULL of a deleted user, `bulk_create`,
  `bulk_update`, and every deletion.

**Retention is intentional.** Registrations are never deleted, and a retired
registration keeps its bed media retained (PROTECT; purge refuses), so every
rendition's provenance stays resolvable. Beds are few and small; that cost is
the price of provenance, not a leak.

## Not in C1 / C2A

Library Track projection, promotion/rollback, stale recall, expiry, Track
reconcile (C2B); any HTTP/recorder surface for spoken content; enabling any
content type;
per-type grants and work queue (Phase E); active messages (Phase D);
occurrence voice tracking (2.6); legacy import or OGRemote changes (Phase F/G).
