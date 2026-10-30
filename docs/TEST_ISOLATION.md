# Test isolation: tests never write to station storage

Post-r0121 correction (roadmap 1.21, P1 data integrity). Companion to
[DEVELOPMENT_DATABASE.md](DEVELOPMENT_DATABASE.md), which isolates the *database*;
this isolates the *filesystem*.

## What went wrong (2026-10-29)

About 172 files in `/srv/isadoraair/waveforms` held the same synthetic 1.77-second flat
envelope (36 points of 255, -20.77 dB), and 165 of them belonged to real tracks. Analysis
writes `<track id>.json` into `WAVEFORMS_DIR`. Several tests (`library` sync-track-file,
`aircheck` recorder and recovery) overrode only `LIBRARY_ROOT`, and the development
worktree `.env` named the station's real directories, so the mocked analysis (1 s of
8 kHz audio at amplitude 3000, analysed at the default 4410 Hz) wrote through
`WAVEFORMS_DIR`. A test database hands out small Track ids that collide with real ones, so
only the files changed, not the real database rows. The 165 waveforms were regenerated
from source audio; the existing cue points were never touched.

## The control

Four layers. None depends on a developer remembering to edit `.env`. The kernel
sandbox is the boundary; the three Python layers inside it are defence in depth and
give precise diagnostics.

### Layer 0: the kernel sandbox (`production/tests/sandbox_exec.sh`)

**Tests run only through `production/tests/run_with_validation_service.sh`.** It runs every
process of a test run inside a bubblewrap sandbox: the preflight, a disposable
PostgreSQL server, the validation and render services, and the test command with every
child it starts (ffmpeg, shells, `psql`, new interpreters, compiled tools). Each process
is a descendant in the same namespaces, so containment does not depend on Python.

* **User namespace**, no capabilities, `no_new_privs`, nested user namespaces disabled.
  Nothing inside can mount, remount or create a mount namespace.
* **Private PID, IPC, UTS and network namespaces.** No host process can be seen or
  signalled; the station's engine, encoders and Gunicorn run as the same Unix user as the
  tests. Only loopback exists, so there is no TCP route to the host's PostgreSQL and no
  abstract Unix socket.
* **Minimal mount view.** A read-only root. `/usr` and `/etc` are read-only, with station
  and service configuration masked empty (`/etc/isadoraair`, `/etc/ssl/isadoraair`,
  `/etc/systemd`, `/etc/nginx`, `/etc/postgresql*`, `/etc/ssh`, `/etc/default`, ...). The
  checkout, its git object store and the interpreter's venv are read-only, and any `.env`
  in them is masked empty. `/tmp`, `/run`, `/var` and `/dev` are fresh and private. Only
  the run's scratch directories are writable, plus, for a service or the test scope, its
  own delegated cgroup. `/srv`, `/opt`, `/var/lib`, `/home/<user>` (apart from those
  read-only code paths), `/run/user` (the systemd user manager and D-Bus) and
  `/run/postgresql` are not there.
* **Allow-listed environment.** `HOME`, `TMPDIR` and `XDG_*` point into the run directory.
  No `PG*` variables or `DATABASE_URL` are passed. The database settings and a per-run
  `SECRET_KEY` come from the run's control directory, which no sandbox mounts.
* **Disposable PostgreSQL.** A cluster created for the run, inside its own sandbox,
  listening only on a Unix socket in the run directory. `isadoraair_dev` is the only role
  that can log in (`CREATEDB`, not a superuser, no memberships). The bootstrap superuser
  has no `pg_hba` entry. The host server and its production role cannot be reached at all.

The launcher refuses (exit 96, nothing started) on any of these: a scratch directory that
is not canonical, owned, 0700 and correctly named; a path overlapping station storage or
the live checkout; a database file that is missing; a socket outside the run directory; a
standard stream that is a directory or inside station storage; or a role used outside its
cgroup context. It closes every other inherited descriptor.

The runner checks each service from outside before it uses it. Every process in the
unit's cgroup must be the outer `bwrap` or inside the sandbox's mount, network and PID
namespaces. Looking through the service process's own root (`/proc/<pid>/root`), `/srv`,
`/opt`, `/var/lib`, `/run/user`, `/run/postgresql` and the live checkout's `.env` must be
absent, and a write into the checkout must fail. The test command runs in a delegated
scope. The sandbox's own processes hold `<scope>/supervisor`, as a service's
`DelegateSubgroup=supervisor` does, and `run_delegated.sh` creates `web` beside it, so the
executors' cgroup tests keep their layout.

```
run_with_validation_service.sh  (host, trusted; creates ctl/run/data/pg in /tmp, never runs test code)
 |- sandbox_exec.sh plain -> bwrap -> initdb / postgres --single / postgres   (data dir + run dir rw)
 |- sandbox_exec.sh plain -> bwrap -> manage.py test_isolation_preflight
 |- systemd-run --user (transient unit, Delegate, DelegateSubgroup=supervisor)
 |     `- sandbox_exec.sh service -> bwrap -> manage.py production_validation_service
 |- systemd-run --user (transient unit)  ... -> manage.py production_render_service
 `- systemd-run --user --scope (Delegate=yes)
       `- sandbox_exec.sh scope -> bwrap -> run_delegated.sh -> manage.py test ... -> ffmpeg, sh, psql, ...
```

### Layer 1: per-run namespace

A test invocation owns one disposable directory, `isadoraair-test-data.XXXXXX`. It is
0700, owned by the invoking user, sits directly inside the system temp base or
`$XDG_RUNTIME_DIR`, and never overlaps station storage. While `isadoraair/settings.py` is
still being imported, `LIBRARY_ROOT`, `WAVEFORMS_DIR`, `PRODUCTION_MEDIA_ROOT`,
`REPORTS_ROOT`, `WEATHER_DATA_DIR` and `ENCODER_STATE_ROOT` are pointed at its
sub-directories. This happens before any app imports, so module-level captures
(`library.storage`, `weather.services`, `encoders.services.lkg`, `webrequests.services`,
`road_conditions.synthesis`) bind to scratch.

A value configured in the environment or `.env` is **not honoured**; one that names a
protected path is printed as a NOTICE. The network ingest URL and key are blanked,
`HOUR_REFILL_INSTALL_ARMED` is forced false, and the production validation/render sockets
are replaced by unreachable namespace paths unless the guarded runner supplies its own.

### Layer 2: Python write boundary

A process-wide audit hook refuses any mutation whose target is inside a protected root,
whatever any setting or `override_settings` says. That covers open for write or create,
remove, rename, mkdir, chmod, link, symlink, copy, move, rmtree, truncate and writable
`mmap`.

* **Resolution.** Symlinks are resolved the way the kernel resolves them: `link/..` is the
  link target's parent, not a lexical no-op. Deleting an *ancestor* of station storage is
  refused too. Removing a symlink itself is allowed, because it never touches the target.
* **Descriptor-relative calls** (`dir_fd=`, `src_dir_fd=`, `dst_dir_fd=`) are judged by the
  directory the descriptor really names. That directory is read from `/proc/self/fd` and
  verified against `fstat` of the descriptor.
* **Descriptor-based calls** (`fchmod`, `ftruncate`, `fdopen` for writing, `mmap`) are
  judged by the file they change.
* **`os.open`.** Its audit event does not carry `dir_fd`, so in a test process `os.open`
  is wrapped. The wrapper resolves the descriptor first, and a relative `os.open` for
  writing that did not come through the wrapper is refused.
* **Hard links.** Linking a protected file anywhere is refused, because a hard link is a
  second, writable name for it.
* **Fail closed.** A target that cannot be resolved and checked is refused (root
  `<unresolvable target>`), never skipped. Examples are a descriptor of a deleted
  directory or an unreadable working directory.
* **Refusals fail the run.** A refusal is a `BaseException` (`ProtectedPathWriteBlocked`),
  so analysis's `except Exception` cannot swallow it. Every refusal is recorded, and the
  runner **fails the run** if any was recorded, even if something swallowed it.

### Layer 3: effective-settings and sandbox proof

This proof runs before any test or database exists. The roots must be inside the
namespace, ingest blank, Hour Refill unarmed and no production sockets configured.
`DB_NAME` and `DB_USER` must both be `isadoraair_dev`, and inside the sandbox `DB_HOST`
must be the disposable server's socket directory. The code must not be the production
checkout. The process must be inside the kernel sandbox, judged from `/proc`, not from
configuration: a user namespace, no capabilities, `no_new_privs`, loopback only, bwrap as
PID 1, the hidden host paths absent or empty, `HOME` in the run directory, no ambient `PG*`
credentials, and no writable mount except declared scratch.

The isolated-CI exemption (`ISADORAAIR_TEST_ISOLATED_CI=1`) applies only to a host with no
installation at all. It never applies inside a user namespace, where an installation is
merely hidden. `isadoraair/test_safety.py` treats the attested sandbox the way it treats
such a host: no installation is visible, and the only reachable server is the disposable
one. The test-name, superuser and identity-query checks still apply.

Protected roots: `/srv/isadoraair`, `/var/lib/isadoraair`, `/opt/isadoraair` (and the
checkout it resolves to), `/opt/isadoraair-runtime`, `/etc/isadoraair`, plus any path the
production environment file names for the six root settings.

### Entry points

| Entry point | Behaviour |
|---|---|
| `production/tests/run_with_validation_service.sh <python> manage.py test ...` | **The supported way.** Builds the sandbox, the disposable database and the namespace; runs the preflight first (nothing else starts if it fails); starts and verifies both services; runs the command sandboxed in a delegated scope; removes every service, process and scratch directory on exit. |
| `manage.py test ...` run directly | `settings.py` engages isolation, and the sandbox check refuses (`KERNEL_SANDBOX_MISSING`) before any namespace, database or test exists. |
| Anything else that loads a test package (`manage.py shell` + unittest, `python -m unittest`) | Every `<app>/tests/__init__.py` calls `test_safety.install()`, which raises `UnsafeTestEnvironmentError` unless isolation is engaged. |

Requirements: `bwrap` (bubblewrap 0.8 or later, for `--disable-userns`), the PostgreSQL
server binaries (`/usr/lib/postgresql/<n>/bin`), and a systemd user manager with cgroup v2
delegation. On Ubuntu the packaged AppArmor profile `bwrap-userns-restrict` is what permits
bubblewrap's user namespace while `kernel.apparmor_restrict_unprivileged_userns=1`. No host
configuration change is needed.

### Writing tests

* A test that exercises `analyze_one_track`, `sync_track_file`, upload or reanalysis should
  override `WAVEFORMS_DIR` with its own scratch directory next to its `LIBRARY_ROOT`
  override. It should assert that `Track.waveform_path` is under it and parse the JSON.
  Join any worker thread, and assert it finished, before the scratch directory is removed.
* A test that deliberately provokes a refusal registers a simulated station directory with
  `test_isolation.protect_for_test(path)` and calls `clear_blocked_writes()` in a cleanup.
  Never aim a provocation at a real `/srv` path.
* An `override_settings` of a root to a protected path is not rejected at override time,
  because some tests read such a literal. Any write through it is refused by layer 2, and
  inside the sandbox the path does not exist.
* Tests no longer see the developer's `.env` (it is masked) or `HOME`. A test that needs a
  setting must set it, for example with `override_settings`.

### Preflight (no test is run, no database is touched)

`run_with_validation_service.sh` runs `manage.py test_isolation_preflight` inside the
sandbox. It prints the effective roots, the database identity, the kernel-sandbox facts,
which stale values were not honoured, and the result of eight non-creating probes against
station paths, all of which must be `refused`. Run outside the sandbox, it refuses.

## Limits (read before trusting it)

* **Not containable here: tests that drive the systemd user manager themselves.**
  `production/tests/validation_service_support.py` (`TransientValidationService`) and its
  users start their own transient units with `systemd-run --user` and read host cgroup
  paths. A unit started that way would run *outside* any sandbox with the user's full
  access, so the sandbox has no user-manager socket and these tests fail loudly
  ("Failed to connect to user scope bus"). They are not skipped. They need a disposable VM
  or host with no station data. The modules are
  `production.tests.test_validation_service`, `test_validation_topology`, `test_gst_taxonomy`
  and `test_validation_admission_config`, `monitoring.tests.test_validation_service_check`,
  and `active_messages.tests.test_managed_worker_unit`.
* **Python-layer residuals, covered only by the sandbox:**
  * writes through a descriptor that was already writable before the guard saw it
    (`os.write`, `sendfile` and `copy_file_range` have no audit event);
  * C extensions and `ctypes`;
  * a directory replaced between the check and the call;
  * a hard link that existed before the run.
* **Sandbox residuals:**
  * The test sandbox inherits the caller's standard streams, a terminal or pipe; directories
    and station-storage files are refused.
  * The services' streams go to the user journal.
  * The run's scratch is shared by the run's own sandboxes. The runner writes nothing into
    it after untrusted code starts, and removes it with `rm -rf`, which does not follow
    symlinks.
  * A SIGKILLed runner leaves its 0700 `/tmp/isadoraair-test-*` directories and possibly
    the disposable `postgres` behind.
  * The sandbox shares the host's cgroup namespace (cgroup paths stay as on a station),
    with `/sys/fs/cgroup` read-only apart from the run's own delegated subtree.
* Tests that open sockets to a real service are unchanged, except that the production
  validation/render sockets are never used.
* A rare legitimate test write into a protected path will now fail loudly. That is
  intended; fix the test to use scratch.

## Read-only integrity check

```bash
manage.py check_waveform_integrity [--waveforms-dir DIR] [--ids ID ...] [--limit N] [--json] \
    [--fail-on-suspect] [--allow-production-read]
```

Lists waveform JSON that cannot be a genuine analysis of its Track: unreadable or non-finite
data, wrong track id, left/right length mismatch, an analysed span far from the Track's known
duration, and the flat, too-short test signature. A short or quiet clip is not accused: a flat
envelope is only a suspect when the span also contradicts the known duration. Files without a
Track are notes. It never calls analysis or repick, never writes or deletes a file, and never
writes to the database. Scanning a station directory requires `--allow-production-read`.
