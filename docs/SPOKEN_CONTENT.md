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
| `save()` adding / `create()` / creating branch of `get_or_create()` / `update_or_create()` | refused outside `transition_scope` | refused outside `transition_scope` | allowed — through the validated, row-locked `save()` |
| `bulk_create()` (plain, `ignore_conflicts`, `update_conflicts`) | **always refused** | **always refused** | **always refused** |
| `save()` of an existing row / updating branch of `update_or_create()` | only title/notes change (compared with the authoritative row) | refused (append-only) | validated, row-locked `save()` |
| `QuerySet.update()` | title/notes; a user FK to NULL alone (Django's SET_NULL) | only `actor=None` (Django's SET_NULL) — every other column refused | **always refused** |
| `bulk_update()` | title/notes only, even in the scope | **always refused** | **always refused** |
| `delete()` (instance, QuerySet, any manager) | **always refused** | **always refused** | refused while items exist (row-locked; PROTECT also refuses) |

A `state` that is not an `ItemState` is refused on every write path, even
inside the scope. The state set is enforced by this boundary, deliberately not
by a database check constraint (a new state must never need a manual
migration). No foreign key into an item or an event cascades, so Django's
deletion collector never reaches them. Admin is read-only for items, events,
assets and editions. Raw SQL is outside the boundary, as for ProductionMedia.

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

`render(source, recipe_key, params)`:
1. recipe from the code registry; parameters validated (unknown, wrong type,
   out of bounds, non-finite, path-like or expression-like values refused);
2. source must be present and valid; beds resolved by key, hash-verified;
3. in a private `work/` directory: prepare (48 kHz stereo), mix, loudness
   analysis (JSON parsed structurally; `-inf` ⇒ `silent_render`), apply +
   FLAC — each a **bounded tool run** (`runner.py`): argv only, fixed tool
   path and environment, `prlimit` (address space, CPU, output size, files,
   no core; missing `prlimit` ⇒ not run), own session killed at the deadline,
   capped output, only the media descriptors passed (`/proc/self/fd/N`);
4. the FLAC goes through the ordinary rendition intake
   (`intake.ingest_derivative`, `kind=rendition`): validated by the validation
   service — ffprobe, full decode **and the engine's GStreamer decode** — and
   recorded with `derived_from`, recipe key/version, the canonical provenance
   digest (`render/provenance.py`: recipe, version, engine version, fully
   resolved params and the exact reference registrations/bytes) and the
   toolchain identity.

Failures raise `RenderError` with a stable code and leave **no bindable
rendition**: nothing is ingested before every step succeeded, an invalid
rendition is refused by intake (no row), a validation-infrastructure failure
leaves an `unvalidated` row nothing can bind; the scratch directory is always
removed; the source is never touched; retries make new renditions.

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

### C2 gate — sandboxed render executor (mandatory before any type is enabled)

Validators run inside the `isadoraair-validation` service's
cgroup/Landlock/seccomp boundary; renders currently run in the calling
process under `prlimit`, on sources that already passed that boundary. That is
acceptable only while C1 is dormant (no production type enabled, projection
closed). Before C2 enables anything, rendering moves to a **separate**
sandboxed render executor: its own service and socket, its own worker pool and
cgroup, Landlock/seccomp, its own admission limits, Monitoring, and an
output-descriptor protocol returning `RenderEvidence`. Its release restarts
services by operator action.

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

## Not in C1

Library Track projection, promotion/rollback, stale recall, expiry, Track
reconcile (C2); worker command; any HTTP/recorder surface for spoken content;
per-type grants and work queue (Phase E); active messages (Phase D);
occurrence voice tracking (2.6); legacy import or OGRemote changes (Phase F/G).
