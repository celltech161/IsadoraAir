# Cue markers: authority, protection and waveform-cache trust (r0123 Package A)

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

Nothing infers authorship. Copying markers to a Track that holds byte-equivalent audio
(`fix_unknown_artists`) keeps them `legacy` and records `copied_from_track`.

New media supersedes protection. A writer that has just published new audio at a Track calls
the authority with `media_replaced=True`: the markers it supplies replace whatever protected
the old audio, and the event is audited ("Cue-marker protection superseded by new media").
These writers are `sync_track_file`, road-conditions synthesis, announcement/dedication
rendering and `prep_mitd_show`.

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

## Waveform cache trust (`library/services/waveform_cache.py`)

`validate(track)` returns one of these verdicts:

* `missing`, `unreadable`, `malformed`: wrong structure, non-finite numbers, envelope range or
  ordering errors, or truncation.
* `wrong_track`, `inconsistent`: analysed duration or marker snapshot disagree with the Track.
* `legacy`: unversioned. Displayable, never trusted.
* `unverified_audio`: v2, but the audio identity can't be proven current.
* `trusted`.

`Track.file_hash` is never evidence of identity. Short clips are valid.

**Temporary restriction:** `AUDIO_IDENTITY_PROVIDER` is unset until Package B, so no cache is
trusted and Repick abstains for every Track. This is intentional. Validation must not be
relaxed to bring Repick back.

## Contract for Package B (analysis/cache consistency)

1. **Basis before decode.** An analyzer reads the marker revision (and
   `analysis_requested_at`) *before* decoding, with no transaction open, and decodes with no
   database transaction and no file lock held.
2. **Stale analyzers lose.** Results go through `apply_calculation(basis_revision=...)`. If
   any marker changed after the basis was read, the whole calculation is discarded and audited.
3. **The cache records what the Track holds.** The cache is written after the authority's
   decision, with the post-decision markers and revision, never values that weren't applied.
   `waveform_path` is recorded only after the file write succeeds. `analysis_requested_at` is
   cleared only if no newer request arrived during the run.
4. **Package B must add:**
   * the audio-identity provider (decoded-audio identity, not `file_hash`);
   * v2 cache writing (`schema`, `schema_version: 2`, `audio_identity`, `analysis`,
     `marker_revision`, `markers`), promoted atomically (temp file plus rename, same
     directory);
   * an identity check at promotion, so a cache computed from replaced audio is never
     promoted;
   * shared locking between concurrent analyzers of one Track, taken only for promotion.

   A legacy cache stays display-only until it is regenerated.
