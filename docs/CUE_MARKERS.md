# Cue markers: authority, protection and waveform-cache trust (r0123 Packages A and B)

Roadmap 1.21. A Track's eight cue markers -- Cue In, Cue Out, Next Start, Intro, Outro,
Hook In, Hook Out and Segue -- are changed in one place, `library/services/cue_markers.py`.
This document is the operating contract; the module docstrings carry the detail.

## Persistent state (migration `library.0099`)

| Column | Meaning |
|---|---|
| `cue_marker_state` (JSON, `db_default {}`) | Per marker: `protection` (`locked`/`unlocked`), `source` (`operator`, `analysis`, `generated`, `unknown`), who and when it was set, approved or unlocked, and `copied_from_track` for copied values. |
| `cue_marker_revision` (bigint, `db_default 0`) | Incremented by every committed change of a marker value or its protection. The optimistic-concurrency token. |
| `analysis_requested_at` (nullable, indexed) | An analysis request, separate from any marker value. |

The migration only adds the three columns: it has no data step, writes no provenance, and
leaves Ready-to-Air unchanged. The two NOT NULL columns have database defaults, so r0122
processes that are still running during an update can insert Tracks. The Update Center probe
therefore classifies those two `AddField` operations as **manual**.

## Protection

| State of the marker | Protection | Analysis may replace it? |
|---|---|---|
| state entry `locked` (operator set or approved it) | protected | no; an operator-requested recalculation may, with an explicit confirmed override |
| state entry `unlocked` (analysis/generator wrote it, or operator unlocked it) | unprotected | yes |
| **no entry, value present** (every marker that existed before r0123: provenance unknown) | **protected (`legacy`)** | no; treated exactly like `locked` |
| value is NULL | `unset` | yes (nothing to destroy) |
| Cue In = 0 on a Track never analyzed (`waveform_path` empty) | `unset` | yes (the column default, not a decision) |
| state entry `review` (Package B: approved for audio that has since been replaced) | protected | no; but it is **not** an approval of the current audio |

Nothing infers authorship.

### Approvals and audio revisions (Package B)

* Every entry records `audio_revision`: the decoded-audio revision (see *Audio identity*
  below) of the file it was set or approved for, taken from a trusted waveform cache. When no
  trusted cache exists it is `null` ("unknown"), and unknown never matches anything.
* New media supersedes protection. A writer that has just published new audio at a Track calls
  the authority with `media_replaced=True` and the new audio revision when it knows it
  (`sync_track_file`, road-conditions synthesis, announcement/dedication rendering and analysis
  all compute it; the speech-splice marker reset and `prep_mitd_show` run before identity is
  known, and pass none).
  * A marker it supplies gets the new value, unlocked.
  * A marker it doesn't supply keeps its value. A `locked` one becomes `review`.
  * Any operator approval involved is retired durably into the marker's `previous_approval`:
    value, approver, approval time, the audio revision it was given for, and the retirement
    time and reason. It is never carried over as an approval of the new audio.
  * Only an approval recorded for exactly the new audio revision stays locked, for example
    when the "new" media is the same decoded sound after a tag-only change.
  * A replaced historical value is kept as `replaced_historical_value`.
  * Each carried value is stamped `audio_changed_at`.
  * Ready-to-Air is never touched. The event is still audited, but the durable record is the
    state, not the SystemEvent.
* An operator edit, approval or unlock of a marker resolves its review and drops
  `previous_approval`.
* `cue_markers.review(row, current_audio_revision)` reports, per marker: the protection, the
  status of its approval (`current`, `other`, `previous` or `unknown` audio) and any retired
  approval. This is for Package C's screens, and `/api/waveform/<id>/` returns it as
  `cue_marker_review`.
* Equivalent media (`fix_unknown_artists`' WAV/AIF→FLAC re-creation) is proven only by equal
  decoded-audio identities of both files, never by path or Track id:
  * proven equal: values carry over with their protection and provenance, marked
    `equivalence.verified`;
  * proven different: the copy is refused;
  * either identity unavailable: values carry over as `legacy`, with any approval only as
    `previous_approval`.
* Rows from before Package B have no `audio_revision` in any entry. Their approvals are
  "unknown audio": still protected, but on media replacement they go to `review`, because the
  absence of a hash is not proof that they apply to new audio.

## Operations

* `operator_edit(track_id, expected_revision=, user=, values=, approve=, unlock=)`: sets
  markers and locks them, approves unchanged markers (locks them without editing), or unlocks
  them. Any revision other than the current one is refused with a conflict.
* `apply_calculation(track_id, values, basis_revision=, ...)`: a calculation that started from
  a stale revision is discarded and audited. Protected markers are preserved and reported, and
  every result is validated.
* Every operation runs in one transaction with `SELECT ... FOR UPDATE` on the Track row,
  validates the **final** marker set (finite, nonnegative, within duration, ordering), commits
  value, state and revision together, and emits its audit event only after commit
  (`transaction.on_commit`). It returns a structured `Result`: `changed`, `preserved`,
  `superseded`, `overridden`, `skipped`, `conflict`, `error`.

The model enforces the authority:

* An existing Track's `save()` never writes marker columns. If an instance's markers changed,
  the save is refused, so a stale instance cannot overwrite newer markers.
* `TrackQuerySet.update`/`bulk_update` refuse marker columns.
* A new Track may be created with marker values; with no state they count as `legacy`.
* Track Admin shows the markers and their state read-only.

The spoken projection keeps its own exact-grant authority over Cue In, Cue Out and Next Start
of a projected Track. The spoken guard refuses every other writer, this authority included,
and that refusal comes back as a structured error.

## Track Detail protocol

* The page sends `cue_marker_revision` and only the markers whose values the operator changed.
* A save that changes no markers does not touch marker state or revision.
* A marker change without a revision, or with a stale one, gets 409 before any other field
  is written. The page keeps the operator's input and says to reload.
* A successful response returns the new revision and values, and the page adopts them.

There is no lock/approve/unlock control yet (Package C). An operator edit locks the marker it
changes.

## Analysis queue, Reset and Repick

* The queue is `analysis_requested_at IS NOT NULL OR next_start_seconds IS NULL`. Reset Analysis
  sets `analysis_requested_at` and no longer nulls Next Start. Nothing destructive is queued.
  Protected markers survive the resulting analysis.
* A detector that finds nothing writes nothing: failed detection never nulls a marker.
* Repick (Track and Category) recalculates only from a **trusted** waveform cache. Otherwise
  it abstains, changes nothing, never writes the cache, and reports the Track as skipped with
  a reason. A Category repick handles each Track independently and returns `repicked`,
  `unchanged`, `skipped`, `skipped_by_reason` and `preserved_by_marker`.
* Package B makes Repick usable again. A trusted cache comes from analysis or from Refresh
  Waveform Only, so no full reanalysis is needed. Verification is bounded: one cache read and
  one `stat` per Track, and no decoding on the request thread (see *Trust re-verification*).
  The calculation goes through the authority against the Track's *current* marker revision.
  It records the cache's audio revision and preserves protected markers.

## Audio identity (`library/services/audio_identity.py`, Package B)

An audio revision is the SHA-256 of a file's **decoded** audio in one canonical form
(`ALGORITHM = isadoraair-audio/v1:pcm_s32le:2ch:native-rate:sha256`):

* **Stream:** the first audio stream (`-map 0:a:0`). Cover art, video, subtitles and data are
  ignored.
* **Sample rate:** the stream's own rate (`ffprobe`), with no resampling. A stream that
  changes rate mid-file is resampled to its first rate, deterministically.
* **Channels:** 2, interleaved L/R. A mono source is upmixed and a multichannel source is
  downmixed by ffmpeg's default rematrix. These are deterministic, but a mono file and a
  dual-mono stereo file are not guaranteed to share an identity.
* **Sample format:** signed 32-bit little-endian. 16- and 24-bit audio is carried losslessly;
  float and lossy decoders are converted deterministically.
* **Hash input:** `b"isadoraair-audio/v1\n<rate>\n2\ns32le\n"` followed by the PCM. The
  cache records the digest, rate, frame count and (for information only) the ffmpeg version.
* **Strict decoding:** a missing audio stream, any ffmpeg error message (`-xerror`), a
  non-zero exit, zero frames or a timeout gives *no* identity, never a partial one.
* **What it ignores and catches:** tag and container changes keep the identity (FLAC and MP3
  are tested), and so does a lossless WAV→FLAC conversion. A changed signal changes it, even
  at the same size and path.
* **Never used as identity:** file names, sizes, times, Track ids and `Track.file_hash`.
* **ffmpeg upgrades:** an upgrade that alters a decoder's output changes identities. Caches
  then fail closed and need a refresh.

Cost:

* **Memory:** the PCM is streamed through the hash in 1 MiB chunks, so memory stays bounded at
  any length.
* **CPU:** about 0.25 s per 4-minute song and about 4 s per hour of programme on the
  development host.
* **Analysis overhead:** the identity is one extra C-speed decode next to the analyzer's
  existing mono and stereo decodes, under 10% of analysis time. It can't share their PCM:
  they run at the configurable analysis rate in 16-bit, and changing them would change
  production cue detection.
* **Storage:** about 300 bytes per cache.
* **Deployment:** none. No rehash or regeneration runs at deployment.

## Waveform cache (`library/services/waveform_cache.py`)

Version 2 adds the following to the legacy fields, which are kept for existing readers
(`samples_left/right`, `envelope_times/db`, `analysis_duration`, `cue_in_seconds`,
`next_start`):

* `schema`, `schema_version` (2) and `track_id`;
* `audio_identity` (or `null` when it couldn't be established);
* `source`, the witness of the file the identity came from: path, device, inode, size,
  `mtime_ns` and `ctime_ns`;
* `analysis`: rate, window and points;
* `generation`: writer and decode start in nanoseconds;
* `markers` and `marker_revision`: the Track's markers *at promotion*.

`validate(track)` returns one of these verdicts:

* `missing`, `unreadable` or `malformed`: wrong structure, non-finite numbers, unequal stereo
  arrays, envelope range, length or order errors, invalid source, analysis or generation
  metadata, a malformed identity, or truncation.
* `incomplete`: a v2 cache with empty stereo arrays, because the analyzer's best-effort stereo
  decode failed. It is stored so the display and envelope behave as before, but it is never
  trusted.
* `unsupported`: a schema or version this code doesn't know.
* `wrong_track`.
* `inconsistent`: one of:
  * the analysed duration disagrees with the Track's duration;
  * the identity's frame count disagrees with the envelope;
  * the marker snapshot claims a newer revision than the database, or different values at the
    same revision.
* `legacy`: unversioned. Displayable, never trusted.
* `unverified_audio`: v2, but either no identity, an unsupported identity algorithm, a
  different file path, or a file changed since (source witness differs).
* `trusted_stale_markers`: the waveform belongs to the current audio, but its marker
  snapshot predates the Track's markers. It is still trusted for Repick, which reads only the
  envelope.
* `trusted`.

Short clips are valid. **Trust re-verification** costs one `stat`: the identity was computed
from the file the witness describes. The kernel-maintained `ctime` changes on every write,
rename, tag edit or metadata change, and no ordinary tool can set it back. Any difference
makes the cache `unverified_audio` until a decode re-establishes the identity: a refresh or
an analysis. A recategorization moves the file, so it also needs a refresh before the next
Repick. That's a deliberate, fail-closed cost.

Readers: `/api/waveform/<id>/` serves any readable cache for display, as before. Its marker
fields (`cue_in_seconds`, `next_start`, `markers`, `marker_revision`) are always the
database's current values; an old snapshot never reaches a client as authoritative. It adds
`trust` (`status`, `trusted`, `markers_current`, `audio_revision`) and `cue_marker_review`,
and leaves out the source witness. `check_waveform_integrity` reads v2 caches unchanged, and
its incident checks and short-clip exclusions are kept.

## Writing caches: one writer, one lock order

Every replacement or removal of a cache file goes through `waveform_cache`:

* `promote(track_id, payload, dir, expected_source=…)`: used by analysis (every
  `analyze_one_track` path) and by Refresh Waveform Only.
* `install_file`: weather publication and Active Messages playout, which analyze into a
  private scratch directory first.
* `restore_file` and `remove`: the announcement renderer's rotation-asset rollback, plus
  Track deletion (the Library view and `remove_track_file`), now done after the row is
  deleted.

Each operation:

1. Takes the per-Track lock: `flock` on `<WAVEFORMS_DIR>/.locks/<id>.lock`. There is one
   location per station, whatever directory is being written, so a publisher's private scratch
   analysis and the real cache serialize too. The lock works across processes and is released
   automatically when its holder dies. Lock files are opened read-only, so one created by
   another service user still works. The wait is bounded at 15 s, then the operation fails with
   `WaveformBusy`; it never waits indefinitely. `.locks` is the one non-cache entry in the
   waveform directory.
2. Re-checks that the Track exists and, for `promote`, that the decoded file is still exactly
   the one decoded (`source_changed`). Whether that file is still the *Track's* file is the
   validator's question: a cache of another path is never trusted. It refuses to replace a
   cache whose decode started later (`superseded`), so a stale analyzer never overwrites newer
   data. It embeds the Track's markers as they stand now.
3. Stages a temporary file in the destination directory (`O_EXCL`, `fsync`), re-reads and
   validates it, and hard-links the current cache as `.<id>.json.prev`, the same-filesystem
   rollback copy.
4. Renames the staged file over the cache and `fsync`s the directory.
5. Drops the rollback copy, unless the caller keeps it for `undo()`: a publisher whose
   database transaction can still fail does, and calls `discard()` after commit.

A failure at any step leaves the previous cache. A crashed writer can leave at most one
temporary file and one `.prev` per Track. Both are dot-files that no reader or checker looks
at, and the next promotion of that Track replaces the `.prev`. Lock files are zero bytes,
one per Track ever written.

**Lock order.** A process holding a cache lock only *reads* the database, so it never waits
for a row lock. Writers that already hold a Track row lock (the publishing transactions) may
take the cache lock, and nothing can deadlock. Nobody decodes audio while holding the cache
lock. Analysis applies its markers in its own short row-locked transaction and only then
promotes.

**Database/cache non-atomicity.** Analysis applies markers first, then promotes. If promotion
fails, the database stays authoritative and the previous cache remains. That cache is either
untrusted for the new audio (its witness no longer matches) or a trusted cache of the same
audio whose marker snapshot is merely stale. Both are detectable, neither authorizes Repick on
other audio, and Refresh Waveform Only or the next analysis repairs it. Analysis applies
nothing if the source file changes while it decodes; a rename between its decodes (a
recategorization) keeps the same inode, size and mtime, so it counts as the same source. No
trusted cache can result without an identity, with an identity whose length disagrees with
the envelope, or with empty stereo samples: those are stored as untrusted v2 caches
(`unverified_audio` or `incomplete`).

## Refresh Waveform Only (Package B)

`POST /api/tracks/<id>/refresh-waveform/` requires `library.manage_tracks` (normal session and
CSRF conventions). `manage.py refresh_waveform <id>` does the same for operators, one Track at
a time; there is no bulk mode, and nothing runs at installation. Both regenerate the cache
from the Track's current file with the analyzer's own decode and compute primitives and the
current AnalysisConfig, and promote it as a trusted v2 cache.

**What it never changes:**

* any Track column: markers, their state and revision, `waveform_path`, duration,
  Ready-to-Air, artist, category or relations;
* play, schedule or as-run records;
* the source file's bytes, tags or format. A WAV or AIF stays WAV or AIF, because the refresh
  never calls `analyze_one_track` or `transcode_lossless_to_flac`.

It performs no database write at all.

**Races:**

* The source witness is taken before decoding and must be unchanged afterwards and at
  promotion. If it changes, the result is `source_changed` (409).
* A marker edit made during the refresh is never overwritten; the cache simply snapshots the
  current markers at promotion.
* A refresh already running for the Track gives `busy` (409).
* A newer analysis that promoted first wins, giving `superseded` (409).

Every failure keeps the previous cache.

**Response:** `track_id`, `status`, `detail`, `replaced`, `previous_cache_retained`, `ok`,
`conflict`, and `old`/`new` objects, each with `status`, `audio_revision`, `started_ns` and
`trusted`.

**Statuses:** 200 `replaced`; 409 `busy`, `source_changed` or `superseded`; 404
`not_found`; 422 `not_analyzed`, `noncanonical_cache_path`, `source_missing`, `decode_failed`,
`audio_unidentifiable` or `failed`.

**Bounded execution:**

* **Up to 900 s of audio:** refreshed within the request, about 3 s per 4-minute song on the
  development host. That keeps it well inside gunicorn's 30 s sync-worker timeout.
* **Longer Tracks (long-form programmes):** return 202 `started`. One detached,
  `nice`-ed `manage.py refresh_waveform --background` process does the work. It inherits two
  locks: the station-wide background slot (one long refresh at a time) and the Track's
  refresh lock. A second request gets 409. The process records its outcome as a SystemEvent.
  It can't block the engine (a separate process), and a service restart that kills it leaves
  the previous cache, because promotion is atomic.

## The incident caches (2026-10-29)

The 165 repaired Track caches and 7 retained orphans are legacy, unversioned files. They stay
displayable and untrusted until an operator chooses to refresh selected Tracks. Nothing in
r0123 regenerates, migrates or deletes caches on its own.
