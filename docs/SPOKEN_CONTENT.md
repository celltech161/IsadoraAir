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

**Phase C1** delivered the permanent schema, the lifecycle authority, the
render engine, the five recipes and reference audio; **C2A** the sandboxed
render executor; **C2B** the library projection (delivery, atomic promotion,
recall, expiry, detach, relocation, reconcile — *Library projection* below).
No production type is enabled.

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
| processing → processed | worker | the current, unexpired lease token; `RenderEvidence` whose rendition is EXACTLY the render the type authorizes now (see *Render completion*); bound |
| processing → failed | worker | the current, unexpired lease token; failure code/detail |
| failed → validated | manager | retry |
| processed → awaiting_review | system | only if the type requires review |
| awaiting_review → rejected | manager | review note |
| pre-delivery → recalled | manager, or the submitter while `recall_policy` allows | not while `processing` |
| processed → delivered | system (type without review) or manager | **only the projection service** (`spoken.delivery`, C2B): exact current provenance; projected first (see *Library projection*) |
| awaiting_review → delivered (`approved_delivered`) | manager | the approval IS the delivery, through the same projection |
| delivered → superseded | system | a newer edition replaced it (current-edition) |
| delivered / superseded → recalled | manager; the submitter only under `owner_anytime` | removes projected content only if it is the asset's current, live content |
| delivered / superseded → expired | system | `expires_at` (delivery + `default_lifetime`) has passed; same rule |
| superseded → delivered (`repromoted`) | — | **never performed in C2B** (no automatic re-promotion) |

`transitions._apply` still refuses every C2 rule (`ProjectionRequired`); the
projection service executes them with a `ProjectedItemWrite` grant whose
projection witness the guard re-checks against the database.

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
2. the executor snapshots every descriptor into a sealed memfd in the same
   pass that proves its bytes, checks the recipe version, the
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

* the caller holds the current, **unexpired** lease;
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

**Executor** (`render/executor.py`): the recipe version must be the
registered one; the pinned references must be exactly the ones the plan
uses; its own canonical digest must equal the client's expectation. Every
received descriptor must be a regular file opened **read-only** — but a
read-only descriptor does not make its inode immutable (another, writable
handle can rewrite it, same size, after any check). So each input becomes an
**immutable service-owned snapshot**: its bytes are copied into a memfd *in
the same read pass* that counts and hashes them (at most one byte past the
claimed size is read), the copy is refused unless exactly the claimed size and
SHA-256 were read (a substituted descriptor is refused before anything runs),
and the memfd is sealed `F_SEAL_WRITE|GROW|SHRINK|SEAL`. The service then
closes the client's descriptors — before the first step — and nothing reads
them again; a later rewrite of the source's or a bed's inode cannot reach the
render. If a reference is re-registered after the snapshot, the render
finishes against its old, pinned bytes and completion refuses it
(`render_outdated`); no database lock is held through a render. Each step is
a fixed ffmpeg argv built from the step library with the pinned absolute
ffmpeg, `-protocol_whitelist fd`, inputs as fresh read-only reopens of
**sealed** memfds only (input snapshots, and each intermediate sealed once its
step finished; anything unsealed is refused) (`-fd N -i fd:`) and outputs as
service-created memfds (`-fd N fd:`) — no path appears anywhere. Snapshots
and intermediates are closed as soon as their last step is done; nothing
persists past the request. One render holds at most `MAX_INPUT_BYTES` (1 GiB)
of input snapshots (`input_too_large` beyond); running out of room while
snapshotting or for an intermediate fails `render_resource_limit` with every
memfd closed and the slot released. Snapshots are memfd pages charged to the
render unit (not reclaimable page cache), alongside the WAV intermediates
(≤ ~660 MiB each for a one-hour stereo source). Measured peaks of the whole
render unit, one render at a time: a one-hour mono source with a 3-minute bed
1.4 GiB; the largest inputs one request may carry (a 512 MiB source and a
512 MiB bed) 1.6 GiB; back to ~130 MiB afterwards — inside `MemoryMax=3G`
with one render at a time, which is why the service renders exactly one (see
*Admission*).

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
`CPUWeight=50` (a render yields to playout); it does not set
`ProtectKernelTunables`/`ProtectControlGroups`, which would make `/sys`
read-only and break the delegated subtree. These unit-level options are
verified by `systemd-analyze verify` only: a user manager cannot apply them,
so the first start under the system manager is an installation check.

**Admission**: exactly **one render at a time**, by construction, and 2
admitted requests waiting (0–4); anything beyond is answered `busy` from the
accept loop without reading the request. Render concurrency is not an
operator choice: one maximal request peaks at ~1.6 GiB of the unit's 3 GiB
(idle ~130 MiB), so two would not fit. `PRODUCTION_RENDER_MAX_ACTIVE`, the
command's `--max-active` and every `RenderService` constructor go through one
parser that accepts only `1`; any other value (including `2`) is refused with
an explanation — never clamped — before the service binds its socket or
allocates anything, so systemd sees a failed start, and a status file
claiming more is not believed. A waiting request holds only its received
descriptors: its input snapshots and intermediates are created only once it
holds the single run slot. A slot is released on success, failure, timeout and
client disconnect (closing the connection cancels the render); the next
waiting request is then promoted; a failed step or a malformed request never
affects the next one. *Deferred (not C2A):* more than one render at once may
return only with admission that reserves each request's expected memory —
sealed source/reference snapshots, decoded/intermediate WAVs, the output, the
service baseline and a safety margin — service-wide (an estimate from the
request's sizes and durations; queue or refuse when the aggregate reservation
would exceed a safe ceiling).

**Sole rendition authority**: a `rendition` row is inserted only by
`render/rendition.py`, for an `ExecutorOutput` the client issued (identity
sealed, single use, rendered from this source, carrying the executor's
identity and SHA-256), and only as **one complete, exact row**. Every concrete
`ProductionMedia` column is classified (`production.models.RENDITION_*`;
a test fails if a new column is not): *bound* — id, kind, custody, storage
key, SHA-256, size, display metadata, parent, recipe key/version, provenance
digest, toolchain — each derived from intake and checked against the executor
output; *initial* — every validation fact (`validation_state=unvalidated`,
`validation_code=""`, `validated_at`, container, codec, sample rate, channels,
decoded/header duration, probe) and retention (`present`, no `purged_at`) at
its canonical unset value; *generated* — `created_at`, which must be an aware
time taken while the authority was open. Inside intake's insertion
transaction (after the parent's binding lock) `rendition_authority(row)` is
opened — refused outside a transaction, and consumable only in the very
transaction block that opened it — and `insert_rendition()` writes exactly
that row with a plain insert that emits **no model signals** (no `pre_save`
receiver can see or widen it), then re-reads and compares **every column**;
any difference refuses, the transaction rolls back and the stored bytes are
removed (the output stays consumed). The authority is not a validation
authority: the validation service's verdict is recorded afterwards by the
ordinary one-time `production.transitions.record_verdict` (an invalid
rendition is refused before anything is stored), or — validator down — only
its infrastructure code, leaving an immutable, unvalidated, unbindable orphan
until `validate_media()` judges it. `ProductionMedia.save()`, `create()`,
reverse managers and every `bulk_create` refuse renditions outright, even
inside an open authority; generic intake refuses `kind=rendition` before
staging a byte. Completion additionally requires the executor's identity in
the rendition's toolchain. Raw SQL is outside the boundary.

**Worker** (`spoken/worker.py`; `manage.py spoken_render_worker [--once]`;
`deploy/isadoraair-spoken-render-worker.service`): claims a validated item (or
one whose lease expired) with `start_processing` — the item lease is the only
authority, no job table — renders, completes with the exact evidence, or
fails. Transient failures (busy, service/sandbox unavailable, timeout, an
unvalidated rendition) retry twice within the lease (2 s, 10 s); one
`render_outdated` re-renders against the current configuration; deterministic
failures fail at once; more than 3 claims fail `attempts_exhausted`; a lease
lost to expiry is left to its new holder. An **expired lease authorizes
nothing**, even before another worker reclaims it: completion and failure
require, under the item lock at one server `now`, a lease, the caller's
token and `lease_expires_at > now` (`== now` is expired; `lease_expired`,
distinct from a replaced token's `stale_lease`). Each render's deadline ends
10 minutes before the lease does (`min(lease remaining − 600 s, 120 s + 6 ×
duration)`), a retry that no longer fits is not started, and a worker whose
lease expired abandons the item to the next claim. There is no lease renewal:
with the 1-hour lease a render gets at most 50 minutes. That is less than the
service's `120 s + 6 × duration` allowance for sources longer than ~8
minutes, but real renders run far faster than that allowance: a one-hour
source with a bed rendered (snapshot, mix, two-pass loudness, FLAC) in about
4 minutes and validated in under 10 seconds on the station host. Operator-attention conditions emit a
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

## Library projection (Phase C2B — `spoken/delivery.py`)

**Authority.** The projection service is the only ordinary writer of
`SpokenAsset`, `SpokenAssetEdition`, the projected library file and the
projection-owned columns of a projected Track. The authoritative chain is
type + item + its validated C2A rendition + asset/active edition → Track;
spoken lifecycle is never inferred from a Track.

* `SpokenAsset` / `SpokenAssetEdition`: guarded querysets (default and base
  manager), `save()`/`delete()` refused, reverse managers derived from them.
  Writes are exact single-use grants executed by `spoken.guard`
  (`AssetCreate` only pending and empty; `AssetWrite` compare-and-write on
  state + generation, permitted state moves only, generation never
  decreases; `EditionCreate` only pending; `EditionWrite` pending → active /
  aborted, active → superseded), each read back complete. Django's own
  SET_NULL of a deleted Track is the one ordinary asset update allowed, and
  only for an asset the deletion already detached.
* A projected Track (one linked to an asset **or occupying an asset's
  committed reserved pathname**) is still a library Track, but
  its `filepath`, `filename`, `format`, `title`, `artist`, `category`,
  `ready2air`, `duration_seconds`, `sample_rate`, `channels`, `bit_depth`,
  cue/next-start marks and `remote_url` belong to the projection: `save()`,
  `update()`, `bulk_update()` (and so `update_or_create`, imports, the track
  edit view — which refuses *before* moving any file) are refused for them
  (`TrackWriteRefused`, via `library.models.register_track_write_guard`).
  Play counts, art, rotation weight and every other column stay the
  library's; ordinary Tracks are untouched. `import_songs` skips
  `spoken-<uuid>.flac`. Losing `SpokenAsset.track_id` therefore does not open
  the Track to ordinary edits or let another Track claim the reserved path.
* A C2 item transition executes only through a `ProjectedItemWrite` whose
  witness the guard re-reads: delivered ⇔ the item's ACTIVE edition on its
  LIVE asset (current item, rendition and generation agreeing) — or a
  retain-only type; superseded ⇔ its edition was superseded by a newer one;
  recalled/expired ⇔ nothing of the item is projected live (no active or
  pending edition). Its event (`ProjectedEventCreate`) follows the write.

**Delivery** (`deliver_item`, `approve_and_deliver`; the worker's
`advance_ready` pass sends `processed` items to review or delivery): lock
type → item → asset; the item must be `processed` (type without review) or
`awaiting_review` (manager approval); the rendition is bound
(`lock_for_binding`), valid, the executor's, and EXACTLY what the type's
current recipe and current references produce (`render_outdated` otherwise —
the item stays `processed`); the target category must exist and form a
safe path. Modes: `current_edition` (`cal_events`, `usd239min`,
`news_item`) — ONE non-retired asset per (type, slot `""`), one stable Track,
generation +1 per delivery; `accumulating` (`3min_ctr`, `lcl_psa`) — one
asset and Track per item; `retain_only` — delivered, nothing projected.
Unknown modes fail closed. Track metadata: the fixed `title_policy`, the
`artist_label` (empty: the type's name), the target category, `ready2air`
= `deliver_ready_to_air`, duration/sample rate/channels from the validated
rendition, cue-in 0 and next-start = the duration (the whole file airs; the
music analyzer's cue/segue detection never runs on it, and no waveform is
generated — a projected spoken Track has no waveform display). Immediately
before actual deck construction the engine re-reads the Track: it must exist,
be Ready-to-Air and have a file. For a reserved spoken path it also requires
the asset to own that Track and be `live`, and the current delivered item,
rendition, generation and active edition to agree. The shared per-asset deck
open lock is held through pipeline synchronization; promotion/reconcile hold
its exclusive form. A cached Track or earlier log-build decision is never
playout authority. Actual air remains proven by the existing
PlayEvent/LogItem as-run paths.
`expires_at` = delivery + `default_lifetime`.

**Idempotency.** Delivering a delivered item returns the existing result (no
generation, Track, edition or file write); recall/expiry of an item already
ended is refused, never repeated; `expire_due` skips what is no longer due;
`reconcile` on a healthy projection writes nothing (no row, no file); a
concurrent duplicate delivery is either the same result or a retryable
`delivery_in_progress`.

**Files.** `LIBRARY_ROOT/<category>/spoken-<asset uuid>.flac` (never from
user input). `spoken/library_files.py` reaches the category directory with
no-follow descriptors (a symlinked or non-directory category is refused),
copies the rendition's immutable bytes from production storage into a hidden
staging file in THAT directory (`.spoken-<uuid>.<random>.partial`,
`O_EXCL|O_NOFOLLOW`) while hashing, fsyncs, re-reads and verifies the staged
copy (rendition SHA == staged SHA), then `renameat`s it over the stable name
— one atomic step on the destination filesystem, whatever filesystem the
production store is on — and fsyncs the directory. Readers see the previous
complete file or the new complete file, never a partial one. A symlink
planted at the stable name is replaced, never followed. Mode 0644.

**The DB/filesystem commit model.** PostgreSQL and a rename are not one
atomic transaction; correctness comes from explicit states, exact hashes and
deterministic reconciliation:

| Step | Authority if the process dies here (after `reconcile`) |
| --- | --- |
| TX-A: asset (first delivery: created `pending`), next generation, `pending` edition | previous active edition (or none); the pending edition is aborted once stale |
| TX-B: re-validate, set Track not Ready-to-Air and asset `retiring`, **commit the unavailable barrier** | previous edition remains DB authority but no new deck may open the stable path |
| TX-C, under type/item/asset/edition locks and the cross-transaction exclusive asset lock: stage + verify + rename | `retiring`; reconcile re-publishes the previous edition's exact bytes (or removes the file when there was none) |
| same TX-C: Track create/update, previous active → superseded, new → active, asset → live at the new generation (compare-and-write), previous item → superseded, item → delivered + events | same rollback to previous authority (TX-C never committed) |
| **COMMIT — the commit point** | the new edition |
| relocation leftover removed, staging pruned | the new edition (leftovers removed by reconcile) |

The exclusive PostgreSQL session advisory lock spans TX-B's commit, file
publication and TX-C's commit. It serializes another delivery and lets
reconcile distinguish a live operation (non-blocking lock attempt fails) from
an abandoned `retiring` barrier (lock succeeds), without a timing heuristic.
Recall and expiry wait across the barrier and then apply to the now-superseded
item without touching the replacement; detach refuses during the gap. The engine takes the
shared form only for a projected deck open: a deck opened before the barrier
may retain A's already-open inode; every new deck after the barrier refuses.
An older in-flight promotion can never overwrite a newer edition. A failure
(not a crash)
aborts the pending edition and converges the files at once. A pending edition
never makes anything airable; there is never more than one active edition
(database constraint + the executor).

**Recall and expiry** (`recall_delivered` — also reached through
`transitions.recall`; `expire_item`, `expire_due` — the worker's bounded,
idempotent sweep). An item leaves delivery with a projection effect ONLY if
asset, item, active edition and current identity agree that it is the live
content; a superseded item's recall/expiry changes its own lifecycle only, so
a stale recall or expiry can never touch a newer edition. Removing live
content: the active edition → superseded, the Track → not Ready-to-Air
(committed first), the file removed after commit; a current-edition asset
becomes `pending` — the same asset and Track wait for a new, deliberate
delivery — and an accumulating asset `retired`. **Nothing is re-promoted
automatically**: `repromoted` is never performed, and
`on_current_recall=restore_previous` is refused by configuration validation.

**Operator Track deletion = detach.** Deleting a projected Track by any path
(instance or queryset; the `pre_delete` receiver runs inside the deletion's
transaction) records the asset as `detached` (active edition superseded, link
cleared; the file is removed after commit). The item keeps its history.
Reconcile never recreates a detached asset's Track; a new delivery to a
detached current-edition slot is refused (`asset_detached`) until a manager
calls `reattach_asset` (→ `pending`; the next delivery creates a new Track).
A detached accumulating asset stays detached.

**Category change = controlled relocation.** A type's new target category
does not move anything silently: the next delivery publishes to the new path,
or `reconcile` relocates — copy + verify + atomic rename into the NEW
directory, then the asset and Track move, then the old file is removed only
after commit. A failed relocation keeps the last known-good file.

**Reconcile** (`manage.py spoken_projection reconcile [--dry-run]`; the
worker runs it at start-up). The database decides what must exist. Per asset
(skipped while an operation holds its locks): abort stale pending editions;
relocate on a category change; the stable path must hold the active edition's
exact bytes (missing or wrong bytes: re-published from the immutable
rendition — never adopted; if that is impossible the Track becomes
unavailable and the operator is told); a pending/retired/detached asset has no
file; an abandoned `retiring` barrier restores the last committed active
edition before atomically returning the Track/asset to airable/live. A live
asset's lost Track link is repaired only when the append-only delivery event
for the exact active edition, generation, path and SHA names one surviving
Track PK. Path equality alone is never identity; a vanished witnessed Track,
missing/ambiguous evidence, or an unrelated Track occupying the reserved path
fails closed and raises operator attention (no guessed adoption/recreation).
When identity is proven, its projection-owned columns are restored from
authority; a non-live asset's Track is never Ready-to-Air. Orphans: stale staging files removed
(fresh ones belong to attempts in flight), an asset's copy at a path it no
longer has removed, an unknown `spoken-*.flac` reported and never deleted.
A healthy projection causes no write. `--dry-run` also emits no SystemEvent
and changes no row, file or mtime; attention is returned in its report only.
`manage.py spoken_projection status`
(`spoken/projection_health.py`) observes the same conditions read-only;
`spoken_projection` SystemEvents mark faults needing the operator. The admin
shows asset mode/state, generation, current item, Track, path, the active
edition's SHA, the observed file status and the edition history (read-only).

**Global lock order** (every operation; a subset, never another order):
SpokenContentType → SpokenItem(s) → SpokenAsset → SpokenAssetEdition(s) →
ProductionMedia (`lock_for_binding`) → ReferenceAudio keys → library Track.
Detach locks the asset alone (still in order). Several ReferenceAudio keys are
always locked through `reference_audio.lock_keys`: ascending lock id (then
key), each id once — a total order even when two keys' ids collide — so a
future multi-reference recipe cannot deadlock (render completion's
`provenance.expected` takes all its keys this way).

**Deliberately not in C2B:** slots other than `""` (one current-edition asset
per type; per-item slots would need a schema field), automatic re-promotion,
a Monitoring check type for projections (a new check-type choice is a
migration; the observer is `spoken_projection status` and the admin),
re-rendering an item whose rendition became outdated before delivery (it
stays `processed`; recall and resubmit), any HTTP/recorder surface, enabling
any production type.

## Not in C1 / C2A / C2B

Any HTTP/recorder surface for spoken content; enabling any content type;
per-type grants and work queue (Phase E); active messages (Phase D);
occurrence voice tracking (2.6); legacy import or OGRemote changes (Phase F/G).
