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
  bounded undo (5 steps / 400 MB), punch-in at the playhead, import, WAV export.
  **Punch-in always records over the take from the playhead** — nothing is
  ever shifted or inserted. The two modes differ only in what follows the new
  audio: *keep the rest of the take after it* (the original tail stays; mode
  and provenance identifier `insert` / `punch-insert`, kept from OGRemote) or
  *discard the rest* (the take ends where the new recording ends; `replace` /
  `punch-replace`). The mode choice is enabled only while punch-in is ticked.
  (r0108 relabelled the controls; the audio semantics are unchanged.)
* **Reopening a workspace with audio already on air** — the editor starts
  empty (opening never loads, drafts, uploads or binds anything) and says so:
  "This voice track is already on air", with **Edit the on-air take**, which
  loads it (unchanged, not dirty — Save stays disabled until an edit). A
  browser draft, if any, is still offered separately.
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

Every validator tool (ffprobe, ffmpeg, the GStreamer parity child) runs inside
a kernel boundary that the tool tree cannot leave, whose **lifetime is owned by
a dedicated systemd service** — never by the web process that asked for it.

**Who owns what.**

```
Gunicorn worker ──(Unix socket, one fixed-shape request, media as an fd)──▶
isadoraair-validation.service   (deploy/isadoraair-validation.service)
   <unit>/supervisor                  the service process
   <unit>/iportal-validation/run-<128-bit id>   one leaf per run
        └─ ffprobe / ffmpeg / GStreamer tree
```

* **The validation service** (`production.services.validation_service`, run as
  `manage.py production_validation_service`) owns the delegated subtree
  exclusively. For each request it creates a fresh leaf, runs the tool there
  (`confinement.execute`), enforces the **hard wall deadline** — min(the
  requested Phase-A tool timeout, the service's own cap for that command) —
  and the CPU-time budget, and destroys the tree when the run ends.
* **The web worker is a client** (`confinement.run_confined`). If it dies —
  or Gunicorn is restarted — the connection closes and the service kills the
  run at once. If it merely stalls, the service's deadline still fires. No
  later request is ever needed for cleanup.
* **If the service dies**, systemd (`KillMode=control-group`) kills every
  process left in the unit's cgroup — validation leaves included — and
  restarts it (`Restart=always`, no start limit). Stopping or restarting the
  service destroys its runs the same way.
* **Start-up cleanup is a readiness gate.** Before it binds its socket, a
  starting instance kills and removes every leftover `run-*` leaf and then
  verifies the subtree is unpopulated (`confinement.reap_all`, all or
  nothing). If the subtree cannot be scanned, a leaf cannot be killed, emptied
  or removed, or anything is still alive afterwards, the process exits
  non-zero **without ever listening**; systemd restarts it and it tries again.
  Clients meanwhile get `confinement_unavailable`.
* **No PID decides ownership.** Leaves are named by a random 128-bit id; the
  subtree belongs to the service alone, so every `run-*` leaf found at
  start-up or stop is a run of this service and is reaped.

**The protocol** (fixed and narrow). A request is
`{"v": 1, "argv": [...], "timeout": seconds}` plus, for a media command,
exactly one regular-file descriptor (SCM_RIGHTS); the media appears in the
argv only as a placeholder and the tool sees `/proc/self/fd/<n>`. The service
runs a request only if its argv is *exactly* one of the five validator
commands (`production.services.validator_commands`: tool version, decoder
check, ffprobe, full decode, GStreamer probe) rebuilt from the **service's
own** tool configuration — so no executable, option, path, environment
variable or limit can be chosen by a client. The socket lives in the service's
`0700` runtime directory (`/run/isadoraair-validation/validator.sock`,
`settings.PRODUCTION_VALIDATION_SOCKET`), is `0600`, and both ends check
`SO_PEERCRED` (same service account). Browsers never reach it. It is not a job
system: one synchronous run per connection.

**Admission is bounded before anything is allocated.** By default at most
**2** runs execute at once and at most **4** more admitted requests wait for a
run slot (within their own deadline). An operator may choose **1–4**
concurrent validations and **0–8** pending requests — never more: these are
hard ceilings of the service (`production.services.admission`, the one
canonical parser for every route). They are set at **Admin → Production
media → Validation limits** (superusers; stored in `.env` as
`PRODUCTION_VALIDATION_MAX_ACTIVE` / `PRODUCTION_VALIDATION_MAX_PENDING`
through the shared managed-settings mechanism, `isadoraair/env_config.py`), or
with the service's `--max-active` / `--max-pending`. Only plain digits inside
the range are accepted: booleans, fractions, signs, hex, exponents, padding,
over-long numbers and out-of-range values are refused — by the admin form
with a message, and by the service itself, which then **does not start**
(never clamps). A saved change takes effect when `isadoraair-validation` is
restarted (which cancels validations in progress; those uploads stay
unvalidated and can be validated again); the admin page compares the saved
values with the limits the running service reports (`admission.json` beside
its socket) and says "Restart required" until they match. The per-run
memory, task, CPU and time limits and the sandbox are not adjustable there.
With the defaults, the accept loop admits a connection only while fewer than
6 are admitted, and only an admitted connection gets a handler
thread; any other connection is answered `{"status": "busy"}` at once and
closed **without reading it** — whatever it sent, passed descriptors
included, is discarded by the kernel with the connection. Beyond the listen
backlog (16) the kernel itself refuses the connect (`EAGAIN`), which the
client also reports as `busy`. An admitted client has
`REQUEST_READ_TIMEOUT_SECONDS` = 5 s **in total** to deliver its request, so a
silent or byte-at-a-time client cannot hold a slot. So the service's threads
(≤ 1 + 6 + 2 output readers per run), connections, parsed requests, media
descriptors and run leaves (≤ 2) are bounded whatever the request volume.
`busy` maps to the retryable infrastructure code `validation_busy` — never a
verdict on the media.

**Guaranteed limits for one run** (defaults calibrated on the real validators
with 10-minute WAV/FLAC/Opus — measured peaks ≤ 22 MiB charged memory, ≤ 18
tasks, ~3 s CPU; overridable only as service configuration,
`settings.PRODUCTION_VALIDATION_LIMITS` or the command's `--limit`):

| Control | Default | Enforced by |
| --- | --- | --- |
| memory, whole tree | 512 MiB | kernel: `memory.max` |
| swap | none | kernel: `memory.swap.max` 0 |
| OOM | kills the whole tree | kernel: `memory.oom.group` 1 |
| tasks (processes + threads) | 64 | kernel: `pids.max` |
| CPU rate | one CPU | kernel: `cpu.max` |
| CPU time, whole tree | 180 s | service monitor (`cpu.stat`, read strictly) |
| wall clock | per tool: 10 s capability checks, 20 s probe, 300 s decode, 125 s GStreamer | service monitor |
| tree destruction | after every run, on client loss, on service stop/death | `cgroup.kill` (service), then systemd |

So one run can never use more than one CPU, nor more than 180 CPU-seconds, nor
live longer than its command's cap — and each bound is enforced by the kernel
or by the service whose own death destroys the tree.

**The CPU-time counter fails closed.** `confinement.cpu_usage_usec` reads the
leaf's `cpu.stat` to end of file and accepts exactly one `usage_usec` line
holding a plain unsigned decimal. A missing or unreadable file, an I/O error,
an incomplete read, an absent, repeated, signed or non-numeric field — or a
counter that goes backwards during the run — ends the run at once: the tree is
destroyed and the result is `confinement_unavailable` (retryable), never zero
use and never a verdict on the media. The service stays up for the retry.

**Per-process backstops**: `RLIMIT_AS` 1 GiB, `RLIMIT_CPU` 180 s (+5 s
SIGKILL), `RLIMIT_FSIZE` 16 MiB, `RLIMIT_NOFILE` 256, `RLIMIT_CORE` 0.

**Why nothing escapes a run.** The trusted, stdlib-only launcher
(`confined_exec.py`) moves itself into the leaf *before* anything untrusted
runs, verifies the move, then sets `no_new_privs`, a **Landlock** policy (read
and execute only — no file is writable anywhere except `/dev/null`, so
`cgroup.procs`/`cgroup.threads` cannot be written; ptrace, signals and
abstract sockets are scoped to the sandbox; no TCP), a **seccomp** filter
(`clone3` → ENOSYS, so `CLONE_INTO_CGROUP` is unavailable; foreign-ABI
syscalls kill) and the rlimits, and only then `exec`s the tool. Cgroup
membership is inherited and is not changed by `setsid()`, `setpgid()`, double
forking or a parent exiting.

**Required kernel facilities — fail closed.** cgroup v2 at `/sys/fs/cgroup`;
the `cpu`, `memory` and `pids` controllers delegated and enabled
(`confinement.establish_root`); in every leaf `cgroup.kill`, `memory.max`,
`memory.swap.max`, `memory.oom.group`, `pids.max` and `cpu.max`, each written
and read back (`confinement._prepare_leaf`); Landlock and seccomp
(`confined_exec.py`, verified before `exec`); a readable CPU-time counter.
Any one missing, or no reachable service → `confinement_unavailable`, a
retryable infrastructure error; the tool never runs outside the boundary. A tool stopped by a limit is
`validation_resource_limit`: a station limit, never an *invalid* verdict.

**Deployment.** Install and enable `isadoraair-validation.service` (rendered
from `deploy/`), then restart gunicorn (its unit now `Wants=` the validation
service and no longer needs any cgroup delegation itself). Until the service
runs, uploads are kept as unvalidated takes that can be re-validated later.

**Monitoring (r0108).** Add a Monitor Check in Admin (Monitoring → Monitor
Checks → Add): kind **Systemd Service**, unit `isadoraair-validation.service`
(consecutive failures 2, the default). No migration seeds it — the same
pattern as the Backup Recovery Assurance check. For this unit the
ordinary systemd probe also asks the service's read-only readiness observer
(`production/services/validation_health.py`): the socket exists, the status
file (`admission.json`) holds in-domain limits, and an *empty-request
handshake* is answered — connect, send nothing, close the writing half; the
service answers `unavailable` ("incomplete request") without running or
logging anything, or `busy` at capacity, so no validation load is created.

| State | Status | Card |
| --- | --- | --- |
| running, answering, valid limits | ok | Running (· busy at capacity), "2 at once · 4 waiting" |
| running, answering, status file missing/malformed | warning | Running · no status / bad status |
| stopped or failed | critical | Stopped |
| between automatic restarts, or restarted since the last poll (`NRestarts`) | critical | Restarting / Restarted |
| active but socket missing, not listening, unreachable, unresponsive (2 s), or a bad reply | critical | Not listening / Unreachable / Unresponsive / Bad reply |
| the observer itself failed | unknown | Unknown |

Debounce, transition events, notifications and cooldowns are Monitoring's
existing ones (a single automatic restart is absorbed by the debounce; a
restart loop alerts). Monitoring never restarts the service, changes its
limits or enters its cgroup, and the card has no Restart button (the unit is
not in the protected operator-restart allowlist). Monitoring and uploads are
independent: either can fail without affecting the other or the engine.

**Tests** that run the real validators use the same topology, built from the
user's own systemd user manager (a transient validation service with the
production unit's properties; the test process in a delegated scope so the
executor's own tests can create leaves like the service does):

    production/tests/run_with_validation_service.sh /path/to/python manage.py test production library ...

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
* **Preview URLs name the take (r0108)** — pages link the preview as
  `/api/voicetrack/<id>/audio/?take=<VoiceTrack.preview_take>` (the bound
  ProductionMedia id, or `legacy`). A rebinding changes the URL, so a browser
  can never replay an earlier take it already loaded under a fixed URL (seen on
  KOGR after a re-record); an unchanged binding keeps a stable URL. A request
  naming a superseded take — a page rendered before the re-record — is
  redirected to the current one *after* the access check: `take` only ever
  selects this row's own current audio, never an arbitrary take. The endpoint
  stays `Cache-Control: private, no-store` (now for legacy files too: the
  logical URL is mutable), Range requests are unchanged, and the Track and
  Voice Tracks pages drop their players' loaded audio when restored from the
  back/forward cache. Inside the studio the on-air player already uses the
  immutable per-take endpoint. Engine resolution is unchanged.
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
