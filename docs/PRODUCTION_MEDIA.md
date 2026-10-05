# iPortal production media (the `production` app)

**Status: Phase A (P1 2.22A) — deployable unused.** Nothing in playout, the
library, scheduling, Voice Tracking, Remote DJ, OGRemote, weather or Update
Center depends on it yet.

iPortal is the product/UI name for IsadoraAir's browser audio-production
workspace. Internally, **what is common across its workflows is the
media-production substrate, not a workflow lifecycle.** The `production` app is
that substrate. It knows nothing about *why* a recording exists or what happens
to it on air.

```
production (this app)         immutable ProductionMedia · storage · validation
   ▲   ▲   ▲                  · open/preview · reconcile · purge safety
   │   │   └── (2.22D) urgent / public-address active messages   — own lifecycle
   │   └────── (2.22C) spoken / produced content                  — own lifecycle
   └────────── (2.22B / 2.6) Voice Tracking (evergreen, occurrence) — own lifecycle
```

Consuming domains reference media through their **own** `PROTECT` foreign key
(never the reverse, never a generic foreign key). There is no `ProductionJob`,
no shared state machine, no approval, due date, recall, or as-run concept here.
A future work queue will aggregate domain work through read models.

## What exists in Phase A

| Piece | Where |
|---|---|
| `ProductionMedia` model, migration `production.0001` | `production/models.py`, `migrations/0001_initial.py` |
| Layout + path generation | `production/services/layout.py` |
| Streaming intake, promotion, derivation | `production/services/intake.py` |
| Validation | `production/services/validation.py`, `gst_probe.py`, `formats.py`, `policy.py` |
| Root safety policy (runtime, backup, restore) | `production/root_policy.py` |
| Explicit state transitions | `production/transitions.py` |
| Safe open / range primitives | `production/services/media_io.py` |
| Purge guard, `lock_for_binding` | `production/services/retention.py` |
| Reconcile / sweeps, `production_reconcile` command | `production/services/reconcile.py` |
| Read-only admin diagnostics | `production/admin.py` |
| Backup / restore | `deploy/stage_production_media.sh`, `restore/40-station-content.sh` |

Not in Phase A: recorder/editor UI, any HTTP endpoint, `VoiceTrack.media`,
spoken-content models, active messages, work queue, capabilities, recipe
*execution*, legacy import, retention *policy*.

## `ProductionMedia`

UUID primary key. Custody: `owner` (nullable, `SET_NULL`) plus an
`owner_username` snapshot. `kind` ∈ `recording`, `upload`, `edit`, `rendition`,
`bed` — what the bytes *are*, never what a workflow does with them.

* **Storage identity** (system generated, frozen): `storage_key`
  (`<2 hex>/<32 hex>`, unique, no extension), `sha256`, `byte_size`.
* **Facts** (written once by validation): `container`, `codec`, `sample_rate`,
  `channels`, `decoded_duration_seconds` and `header_duration_seconds`
  (`Decimal(12,6)`), `probe` (bounded whitelisted JSON — never raw tool output,
  tags or stack traces).
* **Untrusted display metadata:** `original_filename` (basename, control
  characters removed), `declared_content_type`. Never used for identity,
  paths or format decisions.
* **Validation:** `validation_state` (`unvalidated` / `valid` / `invalid`),
  `validation_code`, `validated_at`.
* **Derivation:** `derived_from` (self, `PROTECT`), `recipe_key`,
  `recipe_version`, `recipe_params_digest`, `toolchain`.
* **Retention:** `retention_state` (`present` / `purged`), `purged_at`.

`production.0001` carries these invariants (all `CreateModel`, no `RunPython`,
no seed data): unique `storage_key`; `sha256` is 64 lowercase hex;
`storage_key` has exactly the system-generated shape (so a stored key can never
traverse); `byte_size > 0`; `edit`/`rendition` require `derived_from`;
`present ⇔ purged_at IS NULL`; `validation_state`/`validated_at` agree; a
`valid` row carries its evidence. No other indexes beyond the unique key and
foreign keys: none are needed by Phase A.

### Immutability (the application trust boundary)

Every ordinary Django write path fails closed; deliberate privileged raw SQL is
outside the boundary. There is no database trigger (it would need a manual
migration).

| Path | Behaviour |
|---|---|
| instance `save()` of an existing row | compares every loaded field with the **authoritative row re-read from the database** and refuses any difference -- so a deferred / `only()` / stale instance cannot smuggle a change; an unchanged save writes nothing |
| `QuerySet.update()` -- default manager, `_base_manager`, reverse related managers | refused, **except** exactly `update(owner=None)`: the `SET_NULL` Django performs when a User is deleted (and `user.production_media.clear()`). It only unlinks custody (the `owner_username` snapshot keeps it understandable); reassigning custody is refused |
| `bulk_update()`, `bulk_create(update_conflicts=True)` | refused |
| `update_or_create()` on an existing row | refused (it goes through `save()`) |
| `delete()` -- instance, any queryset, `_base_manager` | refused: purge removes bytes, never rows |

`Meta.base_manager_name = "objects"` makes `_base_manager` (used by the deletion
collector, reverse managers and `Model.save` internals) the same guarded
queryset.

The **only** legitimate state changes are the explicit, state-qualified
transitions in `production/transitions.py`, each a single conditional UPDATE
whose WHERE clause carries the rule:

* `record_verdict` -- facts + verdict, once, on a present `unvalidated` row,
  fact/verdict columns only;
* `record_infrastructure_attempt` -- the code of a failed attempt, on a present
  `unvalidated` row, `validation_code` only;
* `mark_purged` -- `present → purged` with `purged_at`.

There is no `purged → present`, and no way to rewrite identity, bytes identity,
custody, display metadata or provenance. **Editing audio creates a new
`ProductionMedia` with `derived_from` set.**

## Storage

`PRODUCTION_MEDIA_ROOT` (default `/srv/isadoraair/production-media`; read at
call time; deliberately *not* an admin-UI-editable setting, because repointing
it would strand permanent media):

```
media/      permanent immutable bytes      backed up        files 0440, dirs 0750
incoming/   <32hex>.part uploads           not backed up    0600 while streaming
work/       <32hex>/ scratch               not backed up
locks/      lock sidecars                  not backed up
```

Not exposed through nginx. No client-supplied text (filename, title, username,
MIME, URL, form field) ever becomes a path component; every path is generated
from a UUID and re-validated against the exact shape before use.

### Root safety

`production/root_policy.py` is the ONE policy for the root, used by the runtime
(layout, intake, sweeps), the nightly backup and the restore. It is stdlib-only
so the shell tooling runs it with the system `python3`, and it reads `.env`
exactly as python-decouple does (last assignment wins), so tooling judges the
value the runtime will really use. The root is refused when -- lexically
normalized **or** symlink-resolved -- it:

* is empty, relative, contains control characters or `..`, or is `/`;
* equals, contains, or lies inside a system tree (`/etc`, `/usr`, `/bin`,
  `/sbin`, `/lib*`, `/boot`, `/proc`, `/sys`, `/dev`, `/run`, `/snap`, `/root`,
  `/var/log`, ...);
* equals or contains a broad anchor (`/srv`, `/srv/isadoraair`, `/var`,
  `/var/lib`, `/var/lib/isadoraair`, `/home`, `/tmp`, `/mnt`, `/media`, `/opt`,
  any account's home directory, ...);
* equals, contains, or lies inside protected station content or code
  (`LIBRARY_ROOT`, `WAVEFORMS_DIR`, `REPORTS_ROOT`, `WEATHER_DATA_DIR`,
  `ENCODER_STATE_ROOT`, carts, voicetracks, aircheck, rip staging, the
  runtime-recovery payload, the application/repository root, the tooling's own
  checkout, the backup working directory, and any configured value of those).

At every destructive entry point it also applies the **dedicated-directory
rule**: an existing root may hold only `media/`, `incoming/`, `work/`, `locks/`
(plus `lost+found` for a dedicated mount), each a real directory. A genuinely
dedicated directory is accepted anywhere sensible, e.g.
`/srv/isadoraair/production-media` or `/mnt/stationdata/production-media`.

The restore judges the logical root from the restored `.env` **before its first
mutation** (in live and in staged mode alike) and never runs a recursive
ownership change over the configured root. Since r0106 (reconciled onto the
r0106 restore hardening) the root must also pass the restore-time
station-content policy `deploy/restore/content_root_safety.py`, which treats
`PRODUCTION_MEDIA_ROOT` as a managed station root alongside `REPORTS_ROOT` and
`WEATHER_DATA_DIR`: an explicitly empty assignment fails closed, the resolved
path is the one written to, and under `--staging-root` the resolved staged root
may not alias (equal, sit inside, or contain) the resolved staged equivalent of
any other managed root. The root and its four subdirectories are then created
and given owner/mode `0750` one at a time by that helper's `establish`
(no-follow directory descriptors, so an ancestor swapped for a symlink after
validation creates nothing); archive members under
`srv-content/production-media/media/` are validated (plain relative regular
files/directories only) before extraction; and ownership is applied to exactly
the restored members, never recursively. `production/root_policy.py` remains
the runtime/backup authority and is still applied at restore, unchanged.

### Intake ordering: three distinct guarantees

1. **Atomic namespace promotion.** The permanent name appears at once via
   `link(2)` from the fully written staging file and can never overwrite an
   existing permanent file (`EEXIST` is an error).
2. **Process-crash safety.** Bytes become permanent before the row exists:
   a dead process leaves a stale `.part` (no row), an orphan permanent file
   (no row), or a complete row. A committed row never points at missing data.
3. **Sudden-power-loss durability.** The `present` row is committed only after
   this exact sequence has completed:

   | Step | Operation |
   |---|---|
   | a | root, `media/`, `incoming/`: real `0750` directories, each `fsync`ed together with its parent (`layout.ensure_durable_dir`) |
   | b | stream every byte into `incoming/<32hex>.part` (`O_EXCL`, `0600`), hashing and capping |
   | c | `fchmod` the part to its **final** mode `0440`, **then** `fsync` the file |
   | d | ensure the shard `media/<2hex>/` (`0750`), `fsync` the shard and `fsync` its parent `media/` |
   | e | `link(part → media/<2hex>/<32hex>)`, then `fsync` the shard |
   | f | `unlink` the part, then `fsync` `incoming/` |
   | g | one transaction (for a derivative, holding the parent's binding lock): `INSERT` the row, commit |

   Assumptions, and no more: Linux POSIX semantics where `fsync()` of a file
   persists its data and inode metadata and `fsync()` of a directory persists
   its entries (ext4/xfs with default journaling); `incoming/` and `media/` on
   one filesystem; PostgreSQL's own `fsync` enabled; storage that honours cache
   flushes. A power loss between (e) and (g) leaves a durable orphan file and no
   row, which reconciliation removes. `production/tests/test_durability.py`
   proves the order with recorded syscalls; a later reordering fails it.

## Validation contract

Nothing a client says is trusted — not the extension, not the browser MIME, not
a reported duration. Four steps, each shell-free, hard-timed, with bounded
output:

1. **Content sniff** picks exactly one allowlisted ffmpeg demuxer, forced for
   every tool call with only the `file` protocol allowed. An uploaded HLS/concat/
   SDP file therefore never reaches a tool and can never make ffmpeg fetch a URL
   or read another file.
2. **ffprobe** (restricted to that demuxer): topology, container, codec,
   channels, sample rate, header duration.
3. **Full ffmpeg decode** with `-xerror`: the authoritative **decoded duration**
   (from the progress report). Exit code decides, never stderr text (a valid
   browser WebM logs an error-level line yet decodes).
4. **Engine decode:** production's own isolated GStreamer probe
   (`production/services/gst_probe.py`, the same `decodebin` topology playout
   relies on) — proves playout can decode it, with the structured
   media-vs-capability taxonomy below. A consumer that never airs the media may
   skip it (`MediaPolicy`).

**Proven formats** (real ffmpeg-generated fixtures, decoded by both ffmpeg and
GStreamer on this host): WAV/PCM (u8/16/24/32/f32), AIFF/PCM, FLAC, MP3,
Ogg Opus, Ogg Vorbis, WebM/Matroska Opus (including the header-less duration
browsers produce), MP4/M4A AAC, MP4/M4A ALAC. MP3 with attached cover art is
accepted. Limits: 1–2 channels, 8–192 kHz, one audio stream, no video other than
attached pictures, ≤ 4 h, ≤ 512 MiB.

**Two kinds of failure, strictly apart.** A *media verdict* (`invalid` + stable
code, persisted): `empty`, `unreadable_container`, `unsupported_container`,
`unsupported_codec`, `no_audio_stream`, `multiple_audio_streams`,
`video_stream_present`, `invalid_stream_topology`, `channels_out_of_range`,
`sample_rate_out_of_range`, `too_long`, `decode_error`,
`truncated_or_inconsistent`, `empty_audio`, `engine_decode_failed`. An
*infrastructure error* (never persisted as invalid; the row stays `unvalidated`
with the code noted and validation is simply retried): `probe_*`, `decode_*`,
`decoder_unavailable`, `engine_capability_unavailable`, `engine_probe_*`,
`storage_unreadable`, `validation_interrupted`.

**GStreamer parity taxonomy.** Production runs its OWN isolated probe
(`production/services/gst_probe.py`; the library's engine-incident probe is
untouched) and classifies by structured evidence -- GError domain + code,
missing-plugin messages (`GstPbutils`), element-factory failures -- never by
localized text:

| Probe result | Evidence | Mapped to |
|---|---|---|
| `eos` | decoded to end of stream with audio buffers | valid |
| `media_error` | STREAM `DECODE` / `DEMUX` / `FORMAT` / `WRONG_TYPE` / `DECRYPT*`; an audio pad that produced no buffers | **invalid** `engine_decode_failed` |
| `capability_error` | missing-plugin message; CORE `MISSING_PLUGIN` / `NEGOTIATION`; STREAM `CODEC_NOT_FOUND` / `TYPE_NOT_FOUND` / `NOT_IMPLEMENTED`; no audio pad; a required element missing; GI/GStreamer unavailable | infrastructure `engine_capability_unavailable` (retryable) |
| `infrastructure_error` | RESOURCE / LIBRARY errors, generic STREAM `FAILED`, anything unclassified | infrastructure `engine_probe_failed` |
| `timeout` | | infrastructure `engine_probe_timeout` |

The bytes have already passed the allowlist, ffprobe and a full ffmpeg decode,
so a GStreamer that cannot find a decoder for an allowlisted codec is a station
runtime problem, not corrupt media. Proven with real conditions: a decoder
demoted via `GST_PLUGIN_FEATURE_RANK`, an empty plugin registry, an interpreter
without GI, and real undecodable files (`production/tests/test_gst_taxonomy.py`).

**Domain fitness is the consumer's job.** Caller bounds (`MediaPolicy`:
min/max duration, byte cap) refuse the *intake* — no row — and are never
persisted as `invalid`, because the same bytes may suit another consumer.

**Known, deliberate limits.**

* Truncation is detected only where the container carries an expected length:
  WAV, FLAC, AIFF and MP4 fail to decode; MP3 with a Xing header and WebM with a
  duration are caught by decoded-vs-header mismatch (> max(1 s, 5 %)). A
  byte-truncated Ogg is a shorter, self-consistent file and is **not**
  detectable.
* Silence/peak is **not** measured. A silent file is valid media; deciding
  otherwise is recorder/domain policy, and reliable measurement needs
  unbounded tool output or a custom decode pipeline. 2.22B's recorder and 2.22C
  can add it.
* GStreamer evidence is a decode test, not a runtime-capability inventory. A
  host lacking a GStreamer decoder, element or GI is reported as the retryable
  infrastructure error `engine_capability_unavailable` -- never as an invalid
  verdict, and never as a silent pass.

## Open, preview, derive

`open_media(media_id)` is the only way to get a file handle. It takes an
identity (never a path), re-reads the row, refuses purged / unvalidated (unless
asked) / missing / wrong-size media with an explicit `MediaInconsistent`, opens
with `O_NOFOLLOW`, and checks containment in `media/`. `content_type` comes from
the validated container. `parse_byte_range` / `iter_range` are the pure
building blocks for Phase B's range-streaming endpoint (RFC 9110 semantics).
`verify_integrity` re-hashes (slow; diagnostics).

`ingest_derivative(parent, source, kind="edit"|"rendition", recipe_key=…)`
stores a **new** media (own UUID, key, hash); the parent must be present and
valid (judged from the database, not the instance passed in). Recipes are not
executed in Phase A — only their provenance is recorded.

## Purge and retention

**Phase A ships no retention policy**: no scheduler, no timer, no default
lifetime, nothing ages out. Valid bound media is preserved indefinitely.

`purge_media(media)` removes **bytes only** (the row keeps hash, size, facts,
provenance). It refuses while *any* durable row references the media. References
are discovered from Django's relation metadata including **hidden**
`related_name="+"` relations (which `related_objects` omits), whatever the
`on_delete`, and rows hidden by a default manager — so a future domain's
foreign key protects its media with no registration anywhere. Today a media with
a derivative cannot be purged; a later policy may relax that deliberately.

Crash order: the state change commits first, bytes are unlinked after commit
(`transaction.on_commit`). A crash between leaves a purged row with harmless
leftover bytes (swept), never a present row without bytes.

### The binding rule (every reference to ProductionMedia must follow it)

**Any code that creates or changes a durable foreign key / reference to a
`ProductionMedia` must acquire the media's binding lock inside the same
database transaction that writes the reference.** The canonical helper is
`production.services.retention.lock_for_binding`:

```python
with transaction.atomic():
    media = retention.lock_for_binding(media_id)   # row lock + authoritative re-read + present/valid
    voice_track.media = media                      # Phase B: VoiceTrack.media
    voice_track.save()
```

A plain FK assignment is **not** sufficient: Django creates foreign keys
`DEFERRABLE INITIALLY DEFERRED`, so an uncommitted referrer holds no lock on the
media row and a concurrent purge cannot see it (a characterization test in
`test_retention.PurgeRaceTests` records exactly that). `lock_for_binding` takes
the same row lock as `purge_media`: either the purge waits, sees the committed
reference and refuses, or the bind waits, sees the purge and refuses.

Inside Phase A the only reference creator, `ingest_derivative` (`derived_from`),
follows the rule: it streams and validates without any lock, then takes the
parent's binding lock only in the short final transaction that inserts the
child. If the parent was purged meanwhile the bind refuses (`parent_purged`)
and the already-promoted child bytes are removed. Real separate-session tests
cover both orderings, both rollbacks, a stale in-memory parent and a purge
committed mid-stream (`test_binding_race.py`).

For Phase B and later: a consumer must route every binding through ONE service
that does the above -- never a naked FK assignment scattered across views or
models. **GenericForeignKey references and references from another database are
not supported and must not be used**: they are invisible to `find_references`,
so the purge guard cannot protect them. `reconcile.find_purged_media_still_referenced()`
reports any violation.

## Operations

```bash
python manage.py production_reconcile                 # dry run: report only
python manage.py production_reconcile --apply         # remove stale parts/orphans/work
python manage.py production_reconcile --grace-hours 48 --deep   # also re-hash every media
```

Reports: stale `incoming/*.part`; orphan permanent files and leftover bytes of
purged rows; stale `work/*` directories; **present rows whose bytes are missing,
the wrong size or (`--deep`) the wrong hash** (never auto-repaired — that content
can only come from backup); purged media still referenced. Anything not in the
exact system-generated name shape is reported as `unexpected` and left alone.
There is deliberately no timer.

Django admin (`/admin/production/productionmedia/`) is read-only: no add,
change, delete or bulk action, with a "Bytes on disk" check (stat only).

## Backup and restore

`production-media/media/` is durable station content and is in the nightly
archive as `srv-content/production-media/media/` (staged by
`deploy/stage_production_media.sh`, which only ever names `media/`).
`incoming/`, `work/`, `locks/` are never backed up. Restore (stage 40) restores
`media/` with modes preserved, recreates the transient directories empty (`0750`),
honours `PRODUCTION_MEDIA_ROOT` from the restored `.env`, and tells the operator
to run `production_reconcile`. See `docs/DISASTER_RECOVERY.md`.

## Migration safety

`production.0001` is a single `CreateModel` (constraints folded into its
options), no `RunPython`, atomic, additive. It is classified automatic with
zero manual operations by the protected runtime's own probe
(`production/tests/test_migration.py`: exact-plan reconstruction and a live run
with the migration genuinely pending) — no companion authorization needed. Later
schema evolution (e.g. an added constraint) is a legitimate use of the
runtime-11 companion mechanism.

## Carried-forward requirements (from the Phase-A Codex review)

* **Resource confinement before browser exposure (Phase B).** Validation
  subprocesses are shell-free, time-bounded, output-bounded and process-group
  terminated, but they have no memory/cgroup limit. Before any arbitrary
  browser upload is accepted, Phase B must define and prove a confinement
  (e.g. a systemd-run/cgroup memory limit or a dedicated worker).
* **No GenericForeignKey or cross-database references** to ProductionMedia
  (see the binding rule).
* **Commit promptly.** Intake and binding callers must not hide the final row
  commit inside an unexpectedly long outer transaction: until it commits, the
  orphan sweeper sees "no row" (after the 24 h minimum grace).
* **Backup staging growth (before Phase C).** The backup copies `media/` into
  its temporary staging area, so temporary space grows with the store. Fine
  for Phase A volumes; correct it (e.g. stream into the archive) before
  spoken-content volumes grow.
* **Pre-existing, outside iPortal — fixed in r0106:** restore stage 40 used to
  apply `chown -R` to `REPORTS_ROOT` read from the restored `.env` with no
  comparable safety check. r0106 (P0 1.2) replaced it with the shared
  `deploy/restore/content_root_safety.py` validation, no-follow establishment
  and member-scoped ownership, which `PRODUCTION_MEDIA_ROOT`'s restore now
  also uses.
