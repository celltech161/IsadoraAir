# Library reconciliation, duplicate review and relinking (roadmap 3.11, Phase B)

Phase B compares the Library database with the managed media tree, groups possible duplicates for
review, and lets an operator reconnect a missing-media Track to its moved file. It does this when, and only
when, the moved file's decoded audio is provably the Track's.

The governing principle is **recover and reconcile; never delete.** No part of Phase B deletes or moves
media, deletes or merges Tracks, replaces audio, reanalyzes, or changes cue points or readiness.

The local roadmap entry for 3.11 is not in this repository or in `PROJECT_NOTES.md`. This document follows the
Phase B workorder.

## B0: what existed, and what is reused

| Existing code | Contract | Phase B use |
|---|---|---|
| `find_orphaned_tracks` | `--apply` deletes every Track whose path is not a file | **Not used.** Unsafe for an operator workflow: with the Library storage unmounted, every Track is "missing". |
| `find_duplicate_tracks` | Backfills `Track.file_hash` once and never refreshes it. Pairs exact-hash groups and ready-to-air (title, artist, duration ±2 s) groups into `DuplicateCandidate` rows. | **Not run by Phase B.** Its matching rules (±2 s, title/artist) are reused in the scan, which writes no `Track.file_hash`. |
| `apply_duplicate_resolutions` | `--apply` deletes the "drop" Track (and its file) for every `keep_a`/`keep_b` row | **Never invoked.** Safety deficiencies for any future integration (left unchanged here): it does not re-check that the pair is still a duplicate, that the kept Track's file exists and is healthy, or that the decision is current; it has no preview or token; a `ProtectedError` aborts the loop; and it uses the ordinary 60-second freshness grace. |
| `DuplicateCandidate` (`exact`/`probable`, `keep_a`/`keep_b`/`keep_both`) | Admin review records | Phase B shows existing decisions on its groups. It records only **"not a duplicate" (`keep_both`)**, through `get_or_create`, never downgrading or replacing an existing decision. It never records a deletion decision. No choices change, so no migration. |
| `find_category_drift` | `--apply` moves files into `LIBRARY_ROOT/<code>/` | Its folder convention is reused for the read-only **category drift** finding; the scan never moves files. |
| `import_songs`, `sync_track_file` | Register files (`SUPPORTED_EXT`, `spoken-<hex>.flac` excluded) | `SUPPORTED_EXT` and the spoken-name rule are reused. **Not used to relink:** sync refreshes metadata and analysis, which would overwrite a recovered Track's protected edits. Importing an unregistered file stays the existing explicit Import workflow. |
| `track_deletion`, `source_paths` | Commit-safe cleanup; per-path transaction-scoped advisory lock, ordered path → Track row → waveform cache | A relink takes the old and new path locks (sorted), then the Track row lock, so it serializes with registration, recall and post-delete cleanup. The lock order is unchanged. |
| `audio_identity` | Strict decoded-PCM SHA-256; `stat_source` witness (path, dev, ino, size, mtime, ctime) | The relink evidence (below). Decoding happens only in the scan command, never in a web request. |
| `waveform_cache` | v2 cache: `audio_identity` and `source` witness of the file it was made from | Read-only evidence of the Track's last identified audio. Never written by Phase B. |
| Write guards (spoken projection, Active Messages) | Refuse writes to projection-owned or publication-owned paths and columns | A relink is a guarded `TrackQuerySet.update`. The scan also classifies those Tracks and files as **protected**. |
| Phase A preview/apply lessons | Signed token, revalidation under lock, explicit outcomes | The relink uses the same pattern (see below). |

## Ownership boundaries (protected, never reconciled)

These are reported, not acted on:
* Tracks projected from a `SpokenAsset`, and `spoken-<hex>.flac` files.
* Tracks bound to an Active Messages publication (`track` or `track_id_snapshot`).
* Files in the reserved `UrgentPA/am-<hex>.flac` namespace and in Active Messages quarantine.
* Hidden directories (`.am-quarantine`, `.locks`, ...): never descended into.
* Tracks with a `remote_url`, and Tracks whose path lies outside `LIBRARY_ROOT`.

Weather, KanDrive, dedication, aircheck and syndicated-ingest Tracks are ordinary registered Tracks. Their
files are judged like any other, and a recently changed file is reported as **in delivery** (the
60-second rule the deletion cleanup uses), never as stray.

## B1: the scan

`manage.py reconcile_library` is run by an operator, as the service account. It writes
`REPORTS_ROOT/library-reconciliation/`:
* `scan-<id>.json`, the report, written to a temporary file and renamed atomically;
* `latest.json`, a pointer to the last **complete** scan;
* `running.json`, progress with a heartbeat.

An interrupted scan never replaces the last complete report; the page shows it as interrupted. Only one
scan runs at a time (`flock`).

**Read only.** No Track row, file, cue marker, readiness, waveform cache, `file_hash` or
`DuplicateCandidate` row is written.

### Walk
* **Traversal:** `os.scandir` from the resolved `LIBRARY_ROOT`, never following a symlink.
* **Symlinks** (to files or directories) are reported as **uncertain** and not followed.
* **Unreadable** directories and files are **uncertain**.
* **Other file types** are counted by extension, not listed.
* **Metadata only:** one `lstat` per file, no content reads, except the bounded duplicate hashing and the
  evidence decodes below.

### Storage check
* A missing, non-directory or unreadable root aborts the scan with a failed report; no finding is
  produced.
* If at least half of the local Tracks (and at least 20) are missing, the report is marked **suspect**.
  Missing Tracks are then classified uncertain ("Library storage may be unavailable"), never missing.

### Classifications

| Type | Meaning |
|---|---|
| healthy | Counted, not listed: the Track's path is a regular, non-empty file under the root. |
| `missing` | The Track's path does not exist. Absence is never a reason to delete. |
| `unregistered` | A supported audio file no Track names. |
| `moved` | A missing Track with candidate files: same basename, or same byte size as its cache's source witness, with the same extension; at most 5. More than 5 is uncertain. |
| `duplicate` | A group, with a confidence (B2). |
| `category_drift` | The file's first folder under the root is not the Track's primary category code. |
| `shared_source` | Several Tracks or files on one inode (hardlinks), or a Track path that is not canonical (a symlink or `..` alias). |
| `protected` | See ownership boundaries. |
| `in_delivery` | An unregistered or registered file changed within the last 60 seconds. |
| `uncertain` | Symlink, permission or I/O error, zero bytes, changing during the scan, unsupported or unknown evidence, or storage suspect. |

### Bounds
* Tracks are read in 2,000-row chunks (`values_list().iterator()`).
* Files are kept as compact stat tuples.
* Healthy Tracks are counts only.
* Byte hashing only touches stable ordinary files whose size collides with another eligible file's. Protected,
  aliased, unreadable, empty and in-delivery files are excluded before content reads. Each file is opened with
  `O_NOFOLLOW`, bound to the walk's device/inode/size/time witness, read only to that witnessed size in 1 MiB
  chunks under the byte budget (`--max-hash-gb`), and revalidated through both the descriptor and path afterward.
  A race becomes `uncertain`, never exact evidence.
* Decodes only touch relink candidates of Tracks that have a cached identity, under a count budget
  (`--max-decodes`).
* Progress goes to `running.json` and the console.

## B2: duplicate review

| Confidence | Evidence |
|---|---|
| `exact` | Identical SHA-256 of the file bytes, computed in this scan (not `Track.file_hash`) for files of equal size. Hardlinks are `shared_source`, not duplicates. |
| `audio_equivalent` | Same normalized title and artist and duration within 2 s, **and** both Tracks' trusted v2 caches carry the same decoded-audio identity. |
| `likely` | Same normalized title and artist and duration within 2 s, without decoded proof; or the same ISRC with durations within 2 s. |
| `uncertain` | ISRC equal but durations differ (an ISRC collision), same metadata but different decoded audio, or a group larger than 25 Tracks. A large group is reported once. Its pair count is arithmetic; only actual `DuplicateCandidate` rows are read, so nonexistent pairs are never generated. |

Title, artist, ISRC, filename and duration are never treated as proof of identical audio.
`Track.file_hash` is shown as "historical byte hash (may be stale)".

**Existing decisions:**
* A pair already marked `keep_both` is shown as reviewed "not a duplicate" and is not counted as open; a
  later scan never reopens it.
* Pending `keep_a`/`keep_b` decisions are shown with a warning that `apply_duplicate_resolutions` would
  delete a Track.
* Group size, possible-pair count, recorded-decision count and pending-deletion-decision count remain exact.
  The findings API returns at most 50 members/files/decisions per group and 100 expanded members per response,
  with explicit truncation flags and totals; a large group cannot create an unbounded detail query or response.

The page can record "not a duplicate" for a pair (`keep_both`, through `get_or_create`). It never changes a
decision already made.

## B3: explicit relink (the evidence contract)

A relink changes one Track's `filepath` and `filename` (and `updated_at`). It keeps:
* the Track id;
* all metadata, cue points and approvals;
* readiness, play counts, logs and PlayEvents;
* the waveform cache.

It moves, copies and deletes nothing.

### Eligible only when all of these hold
1. The Track's registered file is absent (`lstat` → `ENOENT`, not a permission error), and the scan was not
   suspect.
2. The cache named by the Track's authoritative `waveform_path` is a complete, structurally valid v2 cache,
   was made from **that** path (`source.path == Track.filepath`) and carries a well-formed decoded-audio identity
   of the current algorithm. A blank or invalid reference never falls back to a leftover canonical filename;
   an explicitly recorded alternate cache path is read as the authority.
3. Every audio revision recorded on the Track's cue-point approvals equals that identity, so no evidence
   points at other audio.
4. The candidate is a regular, non-empty file directly under the managed root. It is not a symlink and has
   no symlink in its path. It has a supported extension equal to the Track's, it is not in delivery (older
   than 60 s), no Track owns it, and it is not a spoken, Active Messages or quarantine path.
5. The scan decoded the candidate (strict identity, with the same `stat_source` witness before and after)
   and the identity equals the cache's.

Anything else, including more than one matching candidate, is **manual review** with the reason. No relink
is offered. **The available authority does not cover:** a Track without a cached identity, a cache made from
another path, a candidate in another format, or approvals for other audio. Those need a decode-and-compare
against an independently trusted record, which does not exist yet.

### Preview and apply
* **Preview** revalidates everything it can without decoding:
  * the Track's edit fingerprint as recorded by the scan;
  * the old file is still absent;
  * the candidate's witness is unchanged, so its content is the decoded content;
  * ownership.

  It returns the exact database change and a `django.core.signing` token bound to the operator, the
  finding, the Track's expected state and the candidate's witness. The token lasts 10 minutes.
* **Apply**, in one transaction:
  1. take the old and new path locks (sorted), then the Track row lock;
  2. recheck every condition;
  3. write through the guarded `TrackQuerySet.update`.

  Any difference refuses, and nothing is written. A replay finds the Track already pointing elsewhere and is
  refused. The result is `relinked`, or `refused` with the reason.
* **After a relink,** the waveform cache's witness names the old path. The cache stays displayable but
  **unverified** (Reset Cue Points unavailable) until Reanalyze Track re-decodes. The same decoded identity
  then re-trusts it, and approvals tied to that audio revision stay valid. Phase B does not rewrite caches.

## Operating it

Run a scan as the station's service account, so the web application can read the report:

```
sudo -u <service-user> <venv>/bin/python manage.py reconcile_library [--max-decodes 100] [--max-hash-gb 20]
```

The command prints the counts by type and exits non-zero if the root is unavailable or another scan is
running. Review the findings at **Library → Reconciliation** (`/library/reconciliation/`, track managers only).
* **The page:**
  * scan status: latest complete scan, a failed or interrupted last attempt, a running scan;
  * a chip per finding type;
  * a type filter, a search (a number is a Track id; text matches title, artist or file name) and 25 findings
    per page.
* **Duplicate groups** list a bounded member sample with the exact total group size, directory, size, format,
  categories, ISRC, historical hash and the references a deletion would meet (playlists, rotation inserts,
  upcoming log items, voice tracks, plays). Truncated groups and decision samples are labelled explicitly. One
  displayed pair at a time can be recorded as "not a duplicate"; small groups remain fully reviewable.
* **A relinkable finding** shows "Review relink…": the exact change, what is kept, and the warnings; then
  "Confirm relink"; then the result.

## Measured (guarded sandbox, `measure_reconciliation_performance`)

**Setup:** 20,000 Tracks and 24,050 small files.
* 1,000 Tracks missing;
* 5,000 unregistered files plus 50 byte-identical copies;
* 1,000 files in the wrong category folder;
* 500 metadata pairs.

**Results:**

| Measure | Result |
|---|---|
| Scan time | 9.05 s: walk 1.47, Tracks 5.38, missing/relink evidence 0.09, duplicates 1.62 |
| Database | 5 statements, 0.06 s |
| Memory | 33.8 MB peak traced Python memory, +50.6 MB process maximum RSS |
| Report | 2.75 MB |
| Findings page / search from the API | 12–19 ms |

An adversarial 4,000-Track metadata group (7,998,000 possible pairs) scanned in 1.49 s with five database
statements and 7.5 MB peak traced memory. It examined zero decision rows because none existed; the HTTP response
was 31,783 bytes and expanded 50 members while reporting the exact 4,000-member and 7,998,000-pair totals.

**Production estimates:**
* The Tracks phase is mostly one `realpath`, `lstat` and `access` per Track. Station paths are shallower than
  the sandbox's.
* Byte hashing reads only equal-size files (here 43 MB).
* Each relink-candidate decode costs about 0.25 s per 4-minute song (`audio_identity`), capped by
  `--max-decodes`.
* A cold disk cache on a station will make the walk slower. Run scans outside busy hours; the scan holds no
  database or file locks while it reads.

## Not covered by Phase B

* **No bulk relink, no import of unregistered files** (use the existing Import workflow), and no cleanup or
  garbage collection of any file.
* **No deletion decisions.** Phase B records only "not a duplicate".
* **`apply_duplicate_resolutions` deficiencies remain** (see B0). Integrating duplicate resolution needs its own
  previewed, revalidating design that also handles schedule references.
* **No relink for the unprovable cases:**
  * no cached identity, or a cache made from another path;
  * another format;
  * approvals for other audio;
  * several matching files.

  These need an independent trusted record of the Track's audio.
* **The scan is operator-run.** Nothing schedules it.
