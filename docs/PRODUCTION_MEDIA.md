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
| Validation | `production/services/validation.py`, `formats.py`, `policy.py` |
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

### Immutability

Enforced in Python (no database trigger, which would need a manual migration):
`save()` refuses a changed frozen field; `delete()` and `QuerySet.delete()`
always raise; `QuerySet.update()` accepts only the mutable columns. Identity,
bytes identity, kind, custody, display metadata and provenance never change.
Technical facts and the verdict are written once while `unvalidated`, then
frozen. Retention only moves `present → purged`. **Editing audio creates a new
`ProductionMedia` with `derived_from` set; nothing is ever overwritten.** Rows
are never deleted — purge removes bytes only.

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

### Intake ordering (the crash-safety argument)

1. stream into `incoming/<32hex>.part` (`O_EXCL`, one 1 MiB chunk at a time,
   hashed as it goes, hard byte cap, `fsync`);
2. optionally validate the `.part` — the verdict, or a refusal, is known before
   anything becomes permanent;
3. promote with `link(2)` + `unlink` (atomic, same filesystem, **can never
   overwrite**: `EEXIST` is an error) and fsync the directory;
4. commit the row.

Crash after 1: a stale `.part`, no row. After 3: an orphan permanent file, no
row. After 4: complete and consistent. **A committed, non-purged row always
points at complete bytes.** Orphans and stale parts are reclaimed by
reconciliation after a grace period (minimum one hour, default 24 h).

Callers should commit promptly: a row created inside a long-open caller
transaction looks to the orphan sweeper like "no row yet".

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
4. **Engine decode:** the existing isolated GStreamer probe
   (`library/services/media_gst_probe.py`) — proves playout can decode it. A
   consumer that never airs the media may skip it (`MediaPolicy`).

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
`decoder_unavailable`, `engine_probe_*`, `storage_unreadable`,
`validation_interrupted`.

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
* GStreamer evidence is a decode test, not a runtime-capability inventory; a
  host lacking a GStreamer decoder fails validation (as an engine verdict or an
  infrastructure error) rather than silently passing.

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

### The binding contract (every consuming domain must follow it)

Django creates foreign keys `DEFERRABLE INITIALLY DEFERRED`, so an uncommitted
referrer holds no lock on the media row and a purge cannot see it. **A domain
must call `retention.lock_for_binding(media)` inside the same
`transaction.atomic()` that creates its reference.** It takes the same row lock
as `purge_media`: either the purge waits and then sees the committed reference,
or the bind sees the purge and refuses (`MediaPurged`). This is proven with real
concurrent sessions in `test_retention.PurgeRaceTests`; the same file records,
as a characterization test, what happens to a domain that skips it, and
`reconcile.find_purged_media_still_referenced()` reports it.

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
