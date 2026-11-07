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
| `audio_equivalent` | A likely group whose decoded audio is proved identical. For a pair of two Tracks: both files' full decoded-audio identities, recorded with each file's stat witness (`audio_verification`, below). For a larger group: both Tracks' trusted v2 caches carry the same identity (label only, review only). |
| `likely` | Same normalized title and artist and duration within 2 s, without decoded proof; or the same ISRC with durations within 2 s. A pair records why its audio was not verified. |
| `uncertain` | ISRC equal but durations differ (an ISRC collision), same metadata but different decoded audio (from the same decoder), or a group larger than 25 Tracks. A large group is reported once. Its pair count is arithmetic; only actual `DuplicateCandidate` rows are read, so nonexistent pairs are never generated. |

Title, artist, ISRC, filename and duration are never treated as proof of identical audio.

### Decoded-audio verification of likely pairs (3.11 extension)

A likely pair of exactly two registered Tracks is upgraded only with recorded proof, per file:

* **The identity** is `audio_identity`'s: the SHA-256 of the first audio stream decoded to 2-channel
  `pcm_s32le` at its native rate, with the algorithm, sample rate and frame count. It ignores tags, cover art and
  container; it is not a perceptual fingerprint and has no similarity threshold. Two identities match only when
  algorithm, digest, sample rate and frame count are all equal.
* **Where it comes from.** First the Track's **trusted** waveform cache (`waveform_cache.validate`), and only
  when that cache was made from exactly the walked file: its recorded source path, device, inode, size, mtime
  and ctime equal the scan's. No decode is needed then, in any scan. Otherwise, only when the operator asked for
  verification, a **decode in this scan** of exactly the walked file. The scanner checks the pathname's
  six-field stat witness, opens the file once (read only, `O_NOFOLLOW`), and requires the open file's `fstat`
  to equal the walk's witness. ffprobe and ffmpeg then read **that open file**, inherited as
  `/proc/self/fd/N` (`audio_identity.compute_fd`); they never reopen the Library pathname, so a path that is
  swapped meanwhile (for example a parent directory exchanged and then restored) cannot lend another
  recording's audio. Afterwards the open file and the pathname must both still equal the walk's witness. If the
  tools cannot read the descriptor, the file is unidentified: there is no pathname fallback. A stale,
  unverified, legacy or inconsistent cache is never used.
* **Bound to the file.** The report records, for each file, its path, the walk's six-field witness, the full
  identity and its source (`audio_verification.files`), and the byte relation of the two files
  (`different_size`, `different_bytes`, `identical_bytes` or `not_compared`).
* **Outcomes.** Equal identities: `audio_equivalent`, status `verified`. Different identities from the same
  decoder: `uncertain`, status `different`, never resolvable. Identities from **different ffmpeg versions** that
  disagree prove nothing either way. This is judged on the two identities actually compared, after any missing
  one has been decoded: each cached identity that the running ffmpeg did not make is decoded again if
  verification was requested and its budget allows. When that cannot happen, the pair stays unverified, never
  "different". An exact match stands whatever made the two identities. Anything else (no trusted cache and no decode, an undecodable file, a
  file that changed, a pair already reviewed as not duplicates, an exhausted budget) leaves the pair `likely`
  with status `unverified` and the reason.
* **Bounds.** Off by default: a normal scan decodes nothing for duplicates. Requested with
  `--verify-audio-pairs N` (0 to 100; the page offers 25, 50 or 100).
  * **Pairs.** `N` limits the **pairs that need fresh verification**, meaning any decode at all, whether one
    file or two. It does not count decoder runs. A pair takes its slot before its first decode. A pair proven
    by trusted caches, or by files this scan has already decoded, needs no slot.
  * **Decodes.** Independently, at most `2 × N` decodes. A file in several findings is decoded once. A pair
    starts only when everything it may need fits, including a possible second decode of a cache from another
    ffmpeg version, so a budget never ends with half a pair.
  * **Time.** At most 30 minutes in all (`VERIFY_AUDIO_MAX_SECONDS`). Each file gets at most 5 minutes
    (`VERIFY_DECODE_TIMEOUT_SECONDS`) and never more than what is left of the 30. One `time.monotonic()`
    deadline per file covers ffprobe (at most 60 s of it) and ffmpeg, which gets only what the probe left.
    Nothing starts once a deadline has passed, and ffmpeg is killed and reaped at the limit.
  * **Running.** One decode at a time, streamed (memory bounded by `audio_identity`'s 1 MiB chunks), at the
    scan's priority (`nice 10`, `ionice` idle from the page). Open pairs come first; a pair already recorded
    as "not a duplicate" is never decoded.
  * **Leftovers.** Pairs left over are reported: `budget_exhausted`, split into `skipped_pair_limit`,
    `skipped_decode_limit` and `skipped_time_limit`, plus a note in the report's errors. Running again
    verifies them, since resolved pairs no longer need it.
* **Read only.** Nothing is written but the report: no tag, Track, waveform cache, cue marker or audio file is
  changed. `budgets.audio_verification` records the requested pairs, the pairs that took a slot
  (`pairs_attempted`), the decodes, the seconds, and the verified / different / unverified / budget-exhausted
  counts, with the budget-exhausted count split by limit. A failed scan keeps the last complete report.

Groups of three or more Tracks are never decoded; their trusted caches label them `audio_equivalent` as before,
and they stay review only.
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

## Duplicate resolution from the page (roadmap 3.12)

A small workflow over existing mechanisms (`library.services.library_duplicate_resolution`). It deletes a
Track only after an explicit choice, a preview and a confirmation, and only through the commit-safe
`track_deletion.delete_track` used by every other Track deletion. It never runs `apply_duplicate_resolutions`
and never writes a `keep_a`/`keep_b` row that the command could act on later.

**Eligible findings.** Only a duplicate finding of the latest **complete** scan with **exactly two registered
Tracks** and one of two kinds of proof, each with each file's **stat witness** (the device, inode, size, mtime,
ctime and link count of the scanner's own `FileEntry`, verified around the hash or decode):
* **Exact file match:** `exact` confidence, both files' SHA-256 equal, hashed in that scan.
* **Verified decoded-audio match:** `audio_equivalent` confidence with a complete `audio_verification` record:
  status `verified`, both members, two distinct files, each with its witness and a well-formed identity of the
  current algorithm, and the two identities equal (algorithm, digest, sample rate, frame count). The bytes of the
  two files differ (usually tags); the page says so and never claims that tags, artwork, cue points or other
  Track details are the same.

The kinds are never mixed and nothing stands in for them: a title, artist, ISRC or duration match, an
`audio_equivalent` label without that record, or a recorded mismatch never authorizes a deletion by evidence.
Not resolvable by evidence (only by the operator's manual editorial decision, below), with the reason shown:
groups of three or more Tracks, `likely`/`uncertain` findings, `audio_equivalent`
findings without a verified record, file-only groups, every finding of a `suspect` scan, and exact findings of a
report made before witnesses were recorded (r0126 and earlier: run reconciliation once after installing to
resolve them here). From preview on, both kinds follow the same path: the files must still match the recorded
witnesses at preview and at apply, and every protection, token and bound below applies unchanged. Unregistered files of an eligible group are never touched. Track Detail
links stay in every group for listening and for checking cue points and categories.

**Choice per pair** (the page, default *No action*): keep A and discard B; keep B and discard A; keep both
intentionally (the existing `keep_both` decision, recorded exactly as "Record: not a duplicate" does: once,
never over a decision already made); no action. A is the lower Track id. Nothing is chosen automatically.
Choices belong to the findings page shown. With selections made, a page, filter or search change asks first:
**Stay and keep selections** (the controls return to the page shown) or **Discard selections and continue**; a
search asks once the typing pauses. A newer scan replacing the report clears them with a message saying so;
nothing is ever applied against another scan.

**Preview** (`POST /api/tracks/reconciliation/duplicates/preview/`, at most 25 selections, writes nothing).
For each pair: both Tracks (link, title, artist, file, format, size, categories, playlist / rotation /
current-or-upcoming-log / voice-track references) and a status:

| Status | When |
|---|---|
| Can be deleted | none of the below |
| Needs acknowledgment | the discarded Track has VoiceTracks: deleting it deletes them (Track CASCADE); they are not moved |
| Blocked | the playout engine's state is missing, stale (older than the shared reader's 30 s), unreadable, malformed, has a timestamp that is not a finite number (JSON's `NaN`/`Infinity`) or is in the future, does not report both decks (`A` and `B`; a missing deck is unknown, not empty), or lacks its queue or active-log field; the discarded Track is in a playlist or a rotation slot (PROTECT); is in the log of the current or a later station hour, or of the engine's active log (which can be an earlier hour's); is on a deck or in the engine's queue, was claimed by the engine in the last 3 hours and has not played, or started playing within its duration + 10 minutes; either Track is protected (spoken projection, Active Messages, remote-hosted, non-canonical path), missing, at another path than the scan's, or its file's current stat witness differs from the one the scan hashed it under (modified or replaced); both name the same file; the finding is not eligible; or another selection of the batch names the same finding or Track |

Blocked log entries are listed by date and hour, each linking to `/logs/?date=…&hour=…&return=reconciliation`
(new tab; the Logs page opens that hour and offers a link back). Nothing here edits or regenerates a log; after
editing, **Re-check** previews again. The preview also lists the categories the discarded Track has (primary
and additional) that the kept Track lacks, as suggested **additional** categories, each ticked by default; the
operator can untick any. The kept Track's primary category is never changed and no category is removed.

Keep both needs no engine state (it deletes nothing). Each pair that can proceed gets **its own token**
(`django.core.signing`, bound to the operator, 10 minutes, single use) naming the scan, the pair, both Tracks'
fingerprints (file path and name, format, title, artist, album, duration, primary and additional categories,
and the file's stat witness), the suggested categories and the voice-track count.

**Apply** (`POST /api/tracks/reconciliation/duplicates/apply/`, body `{token, choice, confirm}`) applies **one
pair per request**. The page confirms the batch once, then sends the pairs one at a time in order, showing
"*n* of *m* pair(s) processed"; a blocked, busy or refused pair (a 4xx answer) is reported and the next one is sent; a lost
answer, or any 5xx answer whatever its body (a proxy's 502 can follow a deletion that committed), stops the batch (that pair is *unknown*, the rest *not attempted*) and the findings are reloaded, so an
irreversible action is never retried blindly. A deletion needs `confirm: true`; `choice` carries the chosen
`categories` (a subset of the server's suggestions; nothing else from the client is trusted) and
`acknowledge_voicetracks`. Unticked or cancelled pairs are never sent. The token is spent before the pair is
applied (a `django_session` claim-marker row under the `dupres:` prefix, as bulk edit's: its key can be
inserted once), so a replay, or a second tab, is refused; preview again to retry. Each pair is its own
transaction:

1. observe the engine again (the preview's observation is never relied on); lock both Track rows (lowest id
   first; a new log entry naming either Track waits, then fails on the deleted one);
2. recheck the scan (still the latest, still complete), the finding, both Tracks, files and their stat witness,
   references and on-air evidence, and compare both fingerprints with the preview's: any difference is
   *stale* (preview again);
3. refuse an unacknowledged voice-track deletion, or a voice-track count that changed;
4. add the chosen additional categories to the kept Track, then delete the discarded Track
   (`delete_track(remove_source=True)`). A PROTECT refusal or any failure rolls back that pair's category
   additions too.

A failure on one pair never rolls back another. Every pair is reported:

* `deleted`, with the file-cleanup result after commit:
  * *completed* (removed, or already gone);
  * *retained* (kept for a stated reason: a live owner of the path, replaced, recently changed, busy,
    unverified or non-canonical);
  * *pending*;
  * *failed* (the database deletion committed; remove the file by hand).
* `blocked`, `stale`, `needs_acknowledgment`, `refused`, `failed` or `cancelled`: nothing changed for that pair.
* `recorded` or `already_decided`, for keep both.

Pairs that were not applied stay selected on the page for review and a later preview.

**Request time.** One request does at most one deletion, and every lock wait in it is bounded, each answered
"busy, try again" (nothing changed, or for the cleanup: the file retained and reported): spending the token
1 s, and the best-effort removal of expired claim markers after it another 1 s (its own transaction); the pair's
transaction 3 s in all (`PAIR_LOCK_BUDGET_SECONDS`: Track rows, category rows and the deletion's
cascade together; once spent, a contended lock is refused at once while uncontended statements still run); the
after-commit source-path lock 2 s and waveform-cache lock 2 s (`delete_track(cleanup_lock_timeout=…)`; other
callers keep 5 s and 15 s). The cumulative controllable lock-wait budget is therefore **9 s** per request
(1 + 1 + 3 + 2 + 2), against Gunicorn's 30 s worker timeout. It bounds waiting for locks only, not the request's
total duration: ordinary database and filesystem work (the deletion itself, removing the file) comes on top. Keep
both (also the "Record: not a duplicate" button) waits at most 2 s for the decision row.

**Not changed:** the kept Track's audio file, path, cue markers, waveform, readiness and every other field;
logs; the report. The report is a snapshot: after a resolution the page refreshes the Tracks' current facts
(a deleted Track shows as such) and recommends a new scan with the existing **Run reconciliation** button. It
never starts one by itself.

**Limitations:**
* A decoded-audio match proves the two files decode to the same PCM with this station's ffmpeg; it says nothing
  about tags, artwork, cue points, readiness or other Track details, and nothing of them is merged. A pair whose
  recordings merely sound alike (another master, edit, encode or sample rate) has different identities and
  stays review only. A trusted cache's identity is as good as the cache: caches are written only by the
  station's analysis, through `waveform_cache`.
* The duplicate evidence is the scan's hash or decoded identity and stat witness. Nothing is re-hashed or
  decoded in the web worker. The
  witness detects ordinary modification or replacement (any write changes the ctime, a replacement the inode);
  it is not a cryptographic guard against a privileged user who rewrites the bytes and then resets every
  recorded metadata field.
* "On air" comes from the engine's state file (required fresh and well formed for any deletion) and the
  database: a Track loaded by a path the engine does not report is not seen. With the engine stopped, nothing
  can be deleted here.
* Live fill or an operator insert that picks the discarded Track at the instant of deletion has its insert
  refused; that writer, not this workflow, handles the refusal.
* Holidays, blocked slots, rotation weight, play history and voice tracks are not carried over to the kept
  Track; only additional categories are offered.

### Manual editorial disposition (3.11, final operator-review layer)

What the scan cannot prove -- `likely` and `uncertain` findings, pairs whose decoded audio differs or could not
be decoded, groups of three or more Tracks -- an authorized Library manager can still resolve from the same page,
as **their own editorial decision**. It is a third authority beside the two kinds of proof, kept distinct
everywhere:

| Authority | Established by | Audit `evidence` |
|---|---|---|
| Exact file match | the scan: identical SHA-256 | `exact_bytes` |
| Verified decoded-audio match | the scan: identical decoded-audio identities | `decoded_audio` |
| Manual editorial decision | the operator's review and explicit acknowledgment | `manual_editorial` |

The finding is never relabelled: it stays `likely` or `uncertain`, no hash or identity is made up, and nothing
claims the recordings are the same.

**What the scan records for it.** Every duplicate group of at most 25 Tracks (`DUPLICATE_GROUP_LIMIT`) records
`member_files`: for each member whose file is part of the duplicate comparison (registered, canonical path, not
protected, not shared by another Track, not in delivery, not failed while hashing), its path and the six-field
witness from the walk (device, inode, size, mtime, ctime, link count). This is the walk's own metadata: nothing
extra is read, hashed or decoded. The page never receives these records (the findings API keeps them on the
server). A report made before this version has none and stays review only for this route ("run reconciliation
again"); nothing is reconstructed from names, titles or durations.

**Every member, or no manual deletion.** A manual deletion needs exactly one complete, consistent record for
*every* member of the finding: a well-formed member list (distinct Track ids, at most 25), one record per member
and none for any other Track, each with an absolute path agreeing with its Library-relative form, its size and
mtime, and a six-field witness agreeing with them (at least one link), all naming distinct files. Nothing is
skipped: a member the scan left out as unsafe (for example two members that are one hard-linked file, a protected
or non-canonical path, a file that failed hashing), or a missing, malformed, duplicated or foreign record, makes
the *whole group* ineligible, including its otherwise valid pairs. The page says so ("this finding does not
contain valid file evidence for every Track; review the unsafe or missing members and run reconciliation again
when the source issue is resolved"); **Keep both** stays available. The scanner's exclusion is kept as it is: no
record is made up for an unsafe member.

**On the page.** Such a finding offers **Review & Resolve Manually**, which opens the usual choices for one
explicitly selected pair: *Keep Track A / delete Track B*, *Keep Track B / delete Track A*, *Keep both (not a
duplicate)*, *No action* (the default). In a group of three or more, the operator chooses the two Tracks first
(the same Track cannot be both; a Track deleted since the scan is not offered). The Track Detail links stay, for
listening and checking metadata and cue markers. Choosing a deletion shows:

> These recordings have not been verified as identical. They may contain different edits, masters, audio
> quality or performances. You are authorizing deletion of the selected version based on your own review.

and what the scan established about the audio: that the decoded audio was compared and **differs** (with the
scan's reason), that it was **not verified** (with the recorded reason, e.g. a decode failure), or that it was
**not compared** (larger groups). A separate checkbox acknowledges the editorial decision. It is distinct from
the voice-track acknowledgment, which is still required when the discarded Track has voice tracks. Any change to
a manual choice withdraws a preview on screen. Then the usual **Review selected…**, preview, one confirmation and
apply.

**Server rules** (`library_duplicate_resolution.manual_eligibility`, `_manual_evidence`):
* Offered only where the evidence route is not: a pair the scan proved is resolved with its proof, never
  manually.
* A complete scan; the pair is two different members of that finding, both with recorded files (distinct
  files).
* A selection carries `authority: "manual_editorial"` and `acknowledge_manual: true`. Without the acknowledgment
  the preview lists the pair as blocked and issues **no token**.
* The token names the authority, the acknowledgment and the group's members that exist at preview. Apply
  refuses (400, token not spent) unless the request repeats `acknowledge_manual: true`.
* **Decisions against one group are serialized.** Inside the pair's transaction, apply first locks the rows of
  *every* member of the finding (at most 25; one `SELECT … FOR UPDATE` in ascending Track id order, the same order
  every pair deletion uses, so two requests cannot deadlock on Track rows), within the same lock budget as any
  pair (`PAIR_LOCK_BUDGET_SECONDS`, 3 s for the whole transaction; nothing waits longer). A group it cannot lock
  in time is answered *busy* and nothing changes. Holding the locks, it rechecks that the scan and the finding are
  still the previewed ones and that exactly the members that existed at preview still exist. Any other deletion
  in between -- another pair of the same group, even one applied at the same moment, or a deletion from the Track
  page -- makes the pair *stale*, so a group is reviewed again after each deletion. Only then is the selected
  pair inspected and deleted.
* From there it is the same deletion as above: the files must still match the recorded witnesses at preview
  and apply, and every blocker (playlist, rotation, current/future and active logs, decks, queue, recent
  claims and plays, engine state, ownership, protected resources, voice tracks), the locks and bounds, the
  one-pair transaction with the category additions, the cleanup reporting and the batch behaviour are
  unchanged. The acknowledgment overrides none of them.
* **Keep both** may name a pair of a larger group (`tracks: [a, b]`) and records that pair only (the group stays
  open until every pair is reviewed). It never overwrites a decision already made. A pending legacy `keep_a`/
  `keep_b` decision is still shown and left unchanged; it authorizes nothing.

**Audit.**
* `reconciliation.duplicate_delete` records `evidence: manual_editorial`, `manual_acknowledged`, the finding's
  confidence, what the scan said about the audio (`audio`, `audio_reason`) and the group's members at preview.
* `reconciliation.duplicate_resolution` records `authority: manual_editorial` and the file-cleanup status.

Verified deletions record neither field.

## Operating it

Run a scan as the station's service account, so the web application can read the report:

```
sudo -u <service-user> <venv>/bin/python manage.py reconcile_library [--max-decodes 100] [--max-hash-gb 20] [--verify-audio-pairs 0..100]
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

### Run reconciliation from the page (3.11, minimal launcher)

**Run reconciliation** (track managers only) starts the same command, `manage.py reconcile_library`, with its
defaults. It adds no service, job record, queue or state of its own: the scan keeps its lock, progress file,
reports and status, and the page shows those.

* **The request.** `POST /api/tracks/reconciliation/run/`, CSRF-protected, `library.manage_tracks`. Only one
  thing in it is used: an optional JSON `verify_audio_pairs`, which must be `0` or one of 25, 50 or 100 (`400
  invalid_choice` otherwise, and nothing starts). The confirmation offers it as **Verify audio for likely
  duplicates** (Off by default). A form body is no choice at all.
* **The command.** Fixed: `/usr/bin/nice -n 10 /usr/bin/ionice -c 3 /usr/bin/gnutimeout --signal=KILL 2h
  <venv python> <checkout>/manage.py reconcile_library`, without a shell, plus `--verify-audio-pairs <25|50|100>`
  only when that choice was made. Verification's 30 minutes fit within the 2 hours. It gets the web
  application's own environment and working directory, as the CD-ripping launcher does
  (`library/cd_ripping.py`).
* **The time limit.** GNU `timeout` (coreutils) stays as the scan's parent and leads its process group (the
  session `start_new_session` creates). The scan and every `ffmpeg`/`ffprobe` it starts are in that group.
  * After 2 hours it sends KILL to the scan, then to the whole group, itself included. Everything ends at once,
    and its exit status is "killed by signal 9", never success.
  * Why not TERM first, with a grace period: the scan has no TERM handler, so TERM gains it no cleanup. It
    exits at once, `timeout` exits with it, and the later KILL step never happens. A child that ignored TERM,
    or was slow to act on it, would outlive the scan. KILL loses nothing the scanner could have saved: its
    files are written atomically, and a killed scan leaves at most a hidden `.<name>.<pid>.tmp` file, exactly
    as a TERM or a Gunicorn restart already could.
  * `/usr/bin/gnutimeout` is GNU's timeout. On Ubuntu 25.10 and later, `/usr/bin/timeout` is the uutils (Rust)
    implementation by default, and its KILL escalation (0.8.0) reaches only the direct child, not the group.
    `gnu-coreutils`, which provides `gnutimeout`, is installed with either coreutils flavour. If it were
    missing, the launch fails with 500 `launch_failed` and nothing starts.
  * A timed-out scan ends exactly like an interrupted one (see below): its lock is released, its
    `running.json` stays behind as "stopped before it finished", no attempt is recorded, and `latest.json` and
    the findings stay on the previous complete scan. A scan still starting at the limit is answered (if the
    request is still waiting) as 500 `launch_failed`.
  * **Uninterruptible I/O.** A process blocked in the kernel on storage that has stopped answering ("D" state)
    cannot act on any signal, not even KILL, until that I/O returns or fails. Until then a scan stuck there
    keeps its lock, and the time limit cannot end it sooner. An `ffmpeg`/`ffprobe` stuck there does not hold
    the lock (it never inherits the lock's descriptor).
* **How it runs.** As the web application's service account, in its own session, so the request, the browser
  and the Gunicorn worker can all go away without stopping it. Its output is discarded; errors reach
  Gunicorn's journal.
* **The answer is never a guess.** The request waits up to 2 s for its own process (`timeout`, the leader of
  the new session); it never waits for the scan to finish:

  | What the process did | Answer |
  |---|---|
  | its scan holds the scan lock and has written `running.json`, with a pid in this session | 202 `running`, with `scan_pid` |
  | finished already | 202 `finished`, naming the new attempt (complete, suspect or failed) |
  | exited without scanning, another scan holding the lock | 409 `scan_running` |
  | another request is starting a scan | 409 `scan_starting` |
  | could not be started, or exited without scanning with the lock free | 500 `launch_failed`, with its exit status |
  | none of the above within 2 s | 202 `starting`: launched, not yet confirmed |

  A 202 never means the scan completed. Every 202 also carries what existed before the launch, recorded under
  the launch lock: `previous_attempt` (the last attempt's pointer) and `previous_running` (the pid and start
  time of a progress file left by an earlier scan, if any).
* **Concurrency.**
  * A scan already holding the lock, whether started here or from the command line, refuses the request with
    409, and nothing is started.
  * The launch runs under `.launch.lock`, an empty lock file held only by the request, so a second request at
    the same moment gets 409 `scan_starting`.
  * Whatever happens, the scanner's own lock lets only one scan run, and a command-line scan started during a
    page scan is refused as before.
* **On the page.**
  * The button is disabled while a scan runs or starts. Progress comes from the summary API every 2 s while
    busy, every 15 s otherwise.
  * Only something new counts. A result is an attempt whose `scan_id` differs from `previous_attempt`. A scan
    is a progress file whose pid and start time differ from `previous_running`. An older report or an older
    interrupted scan's progress file is never taken for this launch's.
  * Completion is the new attempt in `last_attempt.json`. A complete or suspect scan reloads the summary and
    the findings once.
  * A failed scan says so: nothing was replaced, and `latest.json` and the findings stay on the previous
    complete scan.
  * A new progress file left behind without a report means the scan stopped before it finished: interrupted,
    or ended by its time limit.
  * After 202 `starting`, the page shows "Starting the scan…" and polls the status every 2 s. If neither a new
    scan nor a new result appears within 30 s, it stops showing "Starting" and says: "Start not confirmed. No
    scan activity or result was detected. Check status before trying again."
    * This does not claim that the scan failed.
    * The button is enabled again; a retry cannot start a second scan (the launch and scan locks).
    * The page keeps polling every 15 s, and a late scan or result is still recognised.
  * No answer to the launch request (the connection failed), or a server error without the launcher's own
    refusal code (for example a proxy's 502): whether a scan started is unknown, so the page does not say "Not
    started". It watches exactly as after 202 `starting`, from the status it showed before the click. If it
    had shown none, it says that the outcome is not known.
  * Answers never arrive out of order on screen. Each status and findings request takes a generation number,
    and its answer is committed (summary, findings, the scan id, the message, the button, the launch) only if
    no newer request was started since. Clicking **Start read-only scan** invalidates status answers already
    in flight and pauses polling until the launch request is answered; polling then resumes, whatever the
    answer.
  * Closing or reloading the page does not affect the scan. After a reload, the page shows a running scan from
    the same status as any other page.
* **Gunicorn restart.** The scan is a process of Gunicorn's service (`KillMode=control-group`, the default).
  `systemctl restart isadoraair-gunicorn` therefore stops it with SIGTERM, and so does an Update Center install
  that restarts Gunicorn. systemd then sends SIGKILL to anything in the control group still running after the
  stop timeout, so no `ffmpeg`/`ffprobe` outlives the service. The scan then ends without a report:
  * its lock is released;
  * its `running.json` stays behind, shown as "interrupted";
  * `latest.json` and the findings are unchanged;
  * it is never resumed or retried. Run it again.

  A Gunicorn worker that only exits or is recycled does not stop the scan (its own session).

**Limitations of the minimal launcher** (each would need infrastructure it deliberately does not have):
* **Tied to Gunicorn.** A scan lives only as long as Gunicorn's service. Any restart, including an install,
  interrupts it; nothing retries.
* **A fixed time limit, no watchdog.** A scan ends after at most 2 hours, however it stalls, except in
  uninterruptible kernel I/O (above), where no signal takes effect until the I/O returns. The limit is not
  adjustable from the page, and a scan that legitimately needs more than 2 hours is stopped as interrupted.
  The scanner's own per-decode timeouts still apply. Ending a scan sooner means restarting Gunicorn, or, as the
  service account, `kill -KILL -- -<pgid>` with the process-group id of `gnutimeout`. A plain `kill` of
  `gnutimeout` forwards TERM to the group, which a child ignoring TERM would survive.
* **Children that leave the group.** The time limit reaches every process in the scan's process group. The
  scanner's `ffmpeg`/`ffprobe` stay in it; a child that made its own session or group would not be reached
  (none does).
* **Shares Gunicorn's resource limits.** It runs at `nice` 10 and idle I/O priority, but inside Gunicorn's
  control group, with no CPU quota or memory cap of its own.
* **No job record.** Who started a scan is logged (Gunicorn's journal), not stored. The page links a launch
  to its result only by what is new since the launch (attempt and progress file); a page that was closed or
  reloaded shows the shared status instead. A command-line scan that starts in the same moment is shown the
  same way, since it is equally new.
* **Zombies until reaped.** A finished scan process stays a zombie until the same Gunicorn worker next starts
  a subprocess, or exits (as with the CD-ripping launcher). A zombie holds no lock and no file.
* **A short wait in the request.** The request occupies one of Gunicorn's three sync workers for up to 2 s
  while it waits for its own process.

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
* **No deletion decisions in Phase B.** Phase B records only "not a duplicate"; deletion of an exact
  two-Track duplicate is the 3.12 workflow above.
* **`apply_duplicate_resolutions` deficiencies remain** (see B0). The 3.12 workflow does not use it and
  writes nothing it acts on.
* **No relink for the unprovable cases:**
  * no cached identity, or a cache made from another path;
  * another format;
  * approvals for other audio;
  * several matching files.

  These need an independent trusted record of the Track's audio.
* **The scan is operator-run.** Nothing schedules it.

## Audit events (r0125)

Bulk edits, relinks and "not a duplicate" decisions are recorded as `SystemEvent` rows (category `library`)
through `library.services.library_audit`. They are shown in /monitoring/ Recent Events and Django admin.

| `action` | Outcomes | Level |
|---|---|---|
| `bulk_edit.apply` | `completed`, `no_change` | info |
| `bulk_edit.apply` | `partial`, `nothing_applied`, `refused` (an expired, replayed, foreign or drifted preview) | warning |
| `reconciliation.relink` | `relinked` | info |
| `reconciliation.relink` | `refused` | warning |
| `reconciliation.not_duplicate` | `recorded`, `already_decided` | info |
| `reconciliation.duplicate_delete` (3.12, one per deleted Track) | `deleted` | info |
| `reconciliation.duplicate_resolution` (3.12, one per apply request, i.e. per pair) | `deleted`, `recorded`, `already_decided` | info |
| `reconciliation.duplicate_resolution` | `blocked`, `stale`, `needs_acknowledgment`, `failed`, `refused` (an expired, spent, foreign, unconfirmed or outdated token) | warning |

**Every event carries:**
* `action_id`, which is also its `dedupe_key` (`library|audit|<action>|<action_id>`), so two operations are never
  coalesced;
* the operator's username and id, and the outcome.

**Per action:**
* `bulk_edit.apply`:
  * the selection mode, requested count and operations;
  * per-outcome counts;
  * at most 20 Track ids per list (updated, and refused/stale/failed/missing), each list with its full count.

  Its outcome is never `completed` while any Track was refused, stale, failed or missing.
* `reconciliation.relink`: the Track id, the scan and finding, and the old and new file paths **relative to
  `LIBRARY_ROOT`**.
* `reconciliation.duplicate_delete`:
  * the kept and deleted Track ids;
  * the evidence: `exact_bytes`, `decoded_audio` or `manual_editorial` (with the acknowledgment, the finding's
    confidence, the scan's audio verdict and the group's members);
  * the scan and finding;
  * the deleted file relative to `LIBRARY_ROOT`;
  * the categories added and the number of voice tracks deleted.

  It is written only if that pair's transaction committed.
* `reconciliation.duplicate_resolution`: the scan, the finding, the resolution (`delete` or `keep_both`) and the
  pair's Track ids, or for a refusal, the reason. A manual editorial deletion also records
  `authority: manual_editorial` and the cleanup status.

**Never recorded:** preview tokens, cookies, request bodies or credentials.

**Timing and reliability:**
* An event is registered with `transaction.on_commit`. It is written only after the work it describes
  committed; a rolled-back operation records nothing.
* A failure to write an event is logged and dropped. It never fails or replays the Library operation.

This is an operational log, not an audit archive. `SystemEvent` writes are best effort, and
`prune_systemevents` removes rows after about 30 days. Nothing here is permanent, guaranteed or append-only.
