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
| processing → processed | worker | the current lease token; a valid `rendition` derived from this item's submission by this type's recipe; bound |
| processing → failed | worker | the current lease token; failure code/detail |
| failed → validated | manager | retry |
| processed → awaiting_review | system | only if the type requires review |
| awaiting_review → rejected | manager | review note |
| pre-delivery → recalled | manager, or the submitter while `recall_policy` allows | not while `processing` |
| → delivered, delivered → superseded / recalled / expired, superseded → delivered | — | **in the table, refused until C2** (`ProjectionRequired`) |

"Manager" in C1 is staff/superuser; per-type grants are Phase E.

**Guards** (`spoken/guard.py`): outside the transition service, ordinary ORM
paths refuse to create items or events, to change any lifecycle column
(`save()` compares with the authoritative row; `update()`/`bulk_update()`
refuse), and to delete items; events are append-only everywhere. Only
title/notes and Django's own `SET_NULL` of a deleted user are free. Admin is
read-only for items, events, assets and editions.

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
   recorded with `derived_from`, recipe key/version, the canonical parameter
   digest (recipe, version, resolved params and the exact reference bytes) and
   the toolchain identity.

Failures raise `RenderError` with a stable code and leave **no bindable
rendition**: nothing is ingested before every step succeeded, an invalid
rendition is refused by intake (no row), a validation-infrastructure failure
leaves an `unvalidated` row nothing can bind; the scratch directory is always
removed; the source is never touched; retries make new renditions.

**Open C2 decision — confined rendering.** Validators run inside the
`isadoraair-validation` service's cgroup/Landlock/seccomp boundary; renders
currently run in the calling process under `prlimit`, on sources that already
passed that boundary. Moving renders into the validation service (a fixed
render-command set plus an output descriptor) is the recommended C2 step
before any production type is enabled; it changes the validation service, so
that release must restart it by operator action (it is outside the protected
restart allowlist).

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
registration). `register()` binds under the binding rule and retires the
previous row for the key (who/why recorded); `resolve()` refuses a missing,
retired, purged, not-valid or hash-mismatched reference and hands the renderer
an open descriptor of the verified bytes. Referenced media — even by retired
rows — cannot be purged, so every rendition's provenance stays resolvable. No
production bed is ingested in C1.

## Not in C1

Library Track projection, promotion/rollback, stale recall, expiry, Track
reconcile (C2); worker command; any HTTP/recorder surface for spoken content;
per-type grants and work queue (Phase E); active messages (Phase D);
occurrence voice tracking (2.6); legacy import or OGRemote changes (Phase F/G).
