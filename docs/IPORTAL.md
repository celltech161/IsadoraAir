# iPortal — Phase B: shared recorder/editor + evergreen VoiceTrack (2.22B)

iPortal is the station's native production workspace. Its rule is unchanged
from Phase A: **what is genuinely common is the media substrate, not a
lifecycle.** Phase B adds the first shared *tool* on that substrate — a
browser recorder/editor — and the first *consumer*, evergreen VoiceTracking,
without giving either a shared workflow.

| Shared (production app) | Not shared (each domain owns it) |
| --- | --- |
| `ProductionMedia`: immutable, validated bytes | evergreen VoiceTrack identity and lifecycle |
| the recorder/editor and its upload/import intake | (later) occurrence VoiceTrack, produced content, active messages |
| validation, now OS-confined | what "save" / "remove" mean for the domain |
| preview / range / export via the safe open | authorization rules for the domain |
| derivation provenance, retention foundation | |

There is **no** persisted recording job, no generic contribution lifecycle, no
GenericForeignKey, no cross-database binding and no shared status machine.

## The reusable contract (`production/recorder/`)

A consumer registers one `RecordingAdapter` under a fixed key from its
`AppConfig.ready()` (`production.recorder.registry.register`). The browser can
only name a registered key and the adapter's own subject parameters — never a
model, an import path, a storage key or a filesystem path.

The adapter supplies:

* `resolve(params)` → the *subject* (for VoiceTrack: `(Track, position)`);
* `context(request, subject)` → a `RecordingContext` rendered by the page and
  **never persisted**: title, purpose, allowed operations, max duration and
  bytes, an opaque optimistic `revision`, the current on-air audio (if any),
  domain display rows, a return URL and a `blocked_reason`;
* `authorize(user, operation)` — server-side, for every operation (`open`,
  `record`, `import`, `edit`, `save`, `remove`, `export`);
* `media_policy(subject)` — the domain's `MediaPolicy` bounds;
* `can_access_media(user, subject, media)` — which UUIDs this user may preview
  or derive from here;
* `source(subject)` — the audio an edit starts from (a ProductionMedia, or a
  legacy file path the domain resolved itself);
* `commit(user, subject, media, expected_revision)` and `remove(...)` — the
  domain's own lifecycle.

The views are mounted **inside the consumer's URL space** with
`production.recorder.urls.recorder_urlpatterns(key, page=..., api=...)`, so the
consumer's existing access rules apply. `/iportal/` is the workspace hub.

The recorder core imports nothing from any consumer (enforced by
`production/tests/test_recorder_core.py`, which runs the whole core through a
scratch non-VoiceTrack adapter).

## The browser workstation

* **Capture** — lossless AudioWorklet float PCM (no codec between the mic and
  the take). Browsers without AudioWorklet fall back to MediaRecorder with a
  MIME type negotiated from an explicit list (`audio/webm;codecs=opus`,
  `audio/ogg;codecs=opus`, `audio/mp4`, `audio/webm`), decoded back to PCM.
* **Operator choices honoured** — echo cancellation and automatic gain control
  are always off (AGC ramps the level down over a take); browser noise
  suppression only when the operator ticks it.
* **Input** — microphone selection (labels appear after permission), level
  meter, gain −15…+15 dB in 0.5 dB steps, **Set Level** (6 s listen, peaks to
  −6 dBFS, below −40 dBFS = "no signal", gain unchanged).
* **Recording** — record, pause/resume, stop, visible elapsed time, a hard
  maximum duration (10 min for VoiceTrack); the take is kept at the limit.
* **Editing** (browser-local) — waveform, zoom, drag selection, playhead,
  keep/delete trim, peak normalize (to 0.95, an explicit operation), gain,
  bounded undo (5 steps / 400 MB), punch-in at the playhead (insert keeps the
  original tail, replace ends the clip with the overdub), import, WAV export.
* **Three visibly distinct states** — **PREVIEW** (the browser-local edit, never
  on air), a saved take (immutable ProductionMedia, not yet on air), and **ON
  AIR** (the domain's committed binding). Preview plays in the browser only:
  it never opens the program output, never acquires a deck, never creates a
  PlayEvent or touches royalty evidence, recency or RDS.
* **Save** — upload the take (exact Content-Length, CSRF) → server validation
  → commit with the context's `revision`. A take imported and saved unedited is
  uploaded byte-for-byte (`upload`); any edit is saved as 16-bit PCM WAV (an
  edit of a ProductionMedia take is a derivative with recipe provenance).
* **Failures** — a rejected file says why; a *retryable* station problem
  (decoder missing, resource limit, …) keeps the take unvalidated and offers
  **Retry validation**; a stale revision shows a conflict ("someone saved a
  newer version") and never overwrites it; a cancelled or interrupted upload
  keeps the local draft.
* **Drafts** — IndexedDB, browser-local, keyed by a server-derived per-user
  namespace + workspace + subject (another person on the same browser profile
  is never offered them; drafts stored without a namespace are pruned), saved
  automatically after each change and before every upload, pruned after 7 days
  and skipped above 300 MB. Reload offers *Restore / Discard*. A draft keeps
  the revision it was made against: saving a restored draft after someone
  else saved is a conflict, never an overwrite (no automatic rebasing).
* **Mobile / iOS** — the microphone is released before playback (an active
  capture track forces earpiece routing); a mono mic is captured mono and a
  silent second channel is collapsed; recording pauses when the page is
  hidden; the device cannot change mid-take and a disappearing device stops
  the take keeping what was captured. Without capture support the page says
  so and offers import only. Server-side correctness never depends on any of
  this.

## Uploads and validation

Bytes stream (1 MiB chunks, exact Content-Length, refused *before reading*
when too large or not audio) straight into `production.services.intake` —
never into memory, `/tmp` or permanent storage directly. A body that ends early
creates nothing. Abandoned partial uploads are reclaimed by the Phase-A
sweeper (24 h grace), triggered at most every 15 minutes by recorder uploads.

### Resource confinement (Phase-B prerequisite)

Every validator tool (ffprobe, ffmpeg, the GStreamer parity child) runs through
`production.services.confinement.run_confined`, inside a kernel boundary that
the tool tree cannot leave and that is destroyed after every run.

**Where.** The web service's own *delegated* cgroup v2 subtree:
`deploy/isadoraair-gunicorn.service` sets `Delegate=cpu memory pids` and
`DelegateSubgroup=web`, so Gunicorn runs in `<unit>/web` and each validation
gets a fresh leaf `<unit>/iportal-validation/run-<pid>-<id>` (or under
`settings.PRODUCTION_VALIDATION_CGROUP`). No privilege, no sudo, no
per-request root: systemd hands the subtree to the service account once, at
unit start.

**Aggregate limits** (the whole tool tree together), defaults calibrated on the
real validators with 10-minute WAV/FLAC/Opus — measured peaks ≤ 22 MiB charged
memory, ≤ 18 tasks, ~3 s CPU:

| Limit | Default | Mechanism |
| --- | --- | --- |
| memory | 512 MiB, no swap, OOM kills the whole tree | `memory.max`, `memory.swap.max` 0, `memory.oom.group` 1 |
| tasks (processes + threads) | 64 | `pids.max` |
| CPU rate | one CPU | `cpu.max` (when the cpu controller is delegated) |
| CPU time | 180 s for the tree | parent monitor on `cpu.stat` |
| wall clock | per tool (Phase A) | parent monitor |

**Per-process backstops**: `RLIMIT_AS` 1 GiB, `RLIMIT_CPU` 180 s (+5 s
SIGKILL), `RLIMIT_FSIZE` 16 MiB, `RLIMIT_NOFILE` 256, `RLIMIT_CORE` 0. All
are overridable with `settings.PRODUCTION_VALIDATION_LIMITS` (a dict of
`confinement.Limits` fields).

**Why nothing escapes.** The trusted, stdlib-only launcher
(`confined_exec.py`) moves itself into the leaf *before* anything untrusted
runs, verifies the move, then sets `no_new_privs`, a **Landlock** policy (read
and execute only — no file is writable anywhere except `/dev/null`, so
`cgroup.procs`/`cgroup.threads` cannot be written; ptrace, signals and
abstract sockets are scoped to the sandbox; no TCP), a **seccomp** filter
(`clone3` → ENOSYS, so `CLONE_INTO_CGROUP` is unavailable; foreign-ABI
syscalls kill) and the rlimits, and only then `exec`s the tool. Cgroup
membership is inherited and is not changed by `setsid()`, `setpgid()`, double
forking or a parent exiting. After every run — success, failure, timeout,
interruption — the parent writes `cgroup.kill` (the kernel SIGKILLs every task
in the leaf, race-free), waits for `populated 0` and removes the leaf; when
`run_confined` returns, no task of that run is alive. Leaves left by a worker
that died mid-run are destroyed by the next run.

**Fail closed.** No delegated subtree, a missing controller, no `cgroup.kill`,
Landlock or seccomp unavailable, a launcher step that cannot be verified, or a
leaf that cannot be emptied → `confinement_unavailable`, a retryable
infrastructure error; the tool never runs outside the boundary. A tool stopped
by a limit is `validation_resource_limit`: a station limit, never an *invalid*
verdict.

**Deployment note.** The unit change takes effect at the next gunicorn
restart after `daemon-reload`; until then (or on a host without cgroup v2
delegation, Landlock or seccomp) uploads are kept as unvalidated takes that
can be re-validated later. Management commands run from a login shell are
not in the delegated subtree and also fail closed.

**Tests** that run the real validators need the same delegated subtree:

    systemd-run --user --scope -p Delegate=yes --quiet \
        production/tests/run_delegated.sh python manage.py test production library ...

Without it they fail (they do not skip): the absence of the boundary must
never go unnoticed.

## Evergreen VoiceTrack adoption

* **Identity unchanged**: `(Track, position)`, unique, one row — never a row per
  take. `VoiceTrack.media` is an additive, nullable `PROTECT` foreign key to
  ProductionMedia (`library.0089`, one `AddField`, classified additive with no
  manual operations by the runtime-11 probe).
* **Studio**: `/voicetracks/studio/?track=<id>&position=intro|outro`, opened from
  the track page and the `/voicetracks/` index; API under
  `/api/voicetrack/iportal/`. Authorization is the existing `voicetrack.record`
  capability; the talent role reaches it with its existing path grants.
* **Binding** — `library.services.voicetrack_media.bind_media` is the only writer
  of `VoiceTrack.media` (`library.voicetrack_guard`). The guard covers the real
  ORM surface: VoiceTrack's *base* manager — the one Django uses for instance
  saves and related managers — is the guarded queryset, which refuses
  `update`, `_update`, `bulk_update`, `_insert`/`bulk_create` (including
  `update_conflicts`) writing the binding; every instance save (`save`,
  `save_base`, `create`, `update_or_create`, related `add`, ModelForm,
  admin) passes `_save_table`, which refuses a change of `media` — deferred
  instances included — and outside the service never writes the column, so a
  stale instance cannot restore an old binding. The scopes are context
  variables (per call stack, reset on exceptions). Raw SQL and hand-built
  `QuerySet(VoiceTrack)` objects are outside any ORM guard. Its short
  transaction: Phase-A `lock_for_binding` (refuses purged / not-valid media) →
  lock the Track and VoiceTrack rows → check the revision → repoint the same
  row → commit. No upload, decode, validation, waveform or transcode work runs
  inside it.
* **Re-record / edit** — a new immutable take; edits record `derived_from`.
  The previous take (or legacy file) is untouched and simply unreferenced
  (reclaimable later by retention policy; Phase B adds no purge UI).
* **Remove from air** — `remove_voicetrack` deletes the VoiceTrack row as
  before, checked against the optimistic revision and audited. It is the only
  direct deletion: `VoiceTrack.delete()`, queryset deletes and the admin are
  refused outside its removal scope. Deleting the parent **Track** still
  cascades to its VoiceTracks (the Track's lifecycle, unchanged), including
  from the Track admin. ProductionMedia bytes are never unlinked — the take
  simply becomes unreferenced for retention; a legacy file is removed only by
  `remove_voicetrack` and only if it lives in the voice-track directory.
* **Legacy compatibility** — `VoiceTrack.playable_audio()` prefers a present,
  valid bound take whose bytes exist, otherwise the legacy file exactly as
  before. No bulk migration: a legacy VoiceTrack becomes ProductionMedia-backed
  only when someone saves it in the studio; viewing never migrates it.
* **Engine** — only media resolution changed: `_vt_maybe_enter` reads path and
  duration from `playable_audio()`. Sequencing, intro/outro timing, ducking,
  minimum gap, the FX sub-mix VT path and restart behaviour are unchanged; the
  engine reads committed rows only, so a new take is heard only after commit.
* **Retired** — the pre-Phase-B `upload`, `save-edited` (both overwrote files
  in place) and `delete` endpoints (all `csrf_exempt`) and the in-page
  recorder/editor. The read-only preview `/api/voicetrack/<id>/audio/` stays.
* **One database** — system check `library.E900/E901` refuses a router that
  splits VoiceTrack and ProductionMedia.

## Backup capacity

The backup copies `media/` into its work area (`/tmp`, RAM-backed on a
station) and builds the archive there, so peak use is about twice the store.
`deploy/stage_production_media.sh` now requires
`available >= 2 x media bytes + reserve` (1 GiB default,
`PRODUCTION_MEDIA_STAGING_RESERVE_BYTES`, accepted range 0..1 TiB) **before
writing anything**, and otherwise exits 4 with the sizes and filesystem. The
arithmetic cannot overflow: every byte count must be a plain decimal of at
most 18 digits, so `2 x media + reserve < 2^63`; a malformed or out-of-range
reserve exits 2, and a failing or implausible `du`/`df` answer exits 4. The backup aborts in its
`production_media` stage and Backup Recovery Assurance reports "last backup
attempt failed at stage 'production_media'". Streaming media straight into the
archive remains the long-term fix before produced-content volumes.

## Not in Phase B

Occurrence-specific VoiceTracking (2.6) and draft-log binding, the work queue,
produced content / news / PSA / underwriting, active messages, any generic job,
talent-assignment changes, OGRemote user import / database migration / cutover
/ retirement, produced-audio recipes, podcast publishing, a general audition or
cue bus, external traffic workflow.
