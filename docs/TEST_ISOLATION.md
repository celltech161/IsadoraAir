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

Three layers, none of which depends on a developer remembering to edit `.env`
(`isadoraair/test_isolation.py`, stdlib only):

1. **Per-run namespace.** A test invocation owns one disposable directory,
   `isadoraair-test-data.XXXXXX` (0700, owned by the invoking user, directly inside the
   system temp base or `$XDG_RUNTIME_DIR`, never overlapping station storage). While
   `isadoraair/settings.py` is still being imported, `LIBRARY_ROOT`, `WAVEFORMS_DIR`,
   `PRODUCTION_MEDIA_ROOT`, `REPORTS_ROOT`, `WEATHER_DATA_DIR` and `ENCODER_STATE_ROOT` are
   pointed at sub-directories of it. This happens before any app imports, so module-level
   captures (`library.storage`, `weather.services`, `encoders.services.lkg`,
   `webrequests.services`, `road_conditions.synthesis`) bind to scratch. A configured value
   from the environment or `.env` is **not honoured**; one that names a protected path is
   printed as a NOTICE. The network ingest URL and key are blanked, `HOUR_REFILL_INSTALL_ARMED`
   is forced false, and the production validation/render sockets are replaced by unreachable
   namespace paths unless the guarded runner supplies its own.
2. **Write boundary.** A process-wide audit hook refuses any mutation (open for write or
   create, remove, rename, mkdir, chmod, symlink, copy, move, rmtree, ...) whose resolved
   target is inside a protected root, whatever any setting or `override_settings` says.
   Symlinks and `..` are resolved first. Deleting an *ancestor* of station storage is refused
   too; removing a symlink itself is allowed because it never touches the target.
   The refusal is a `BaseException` (`ProtectedPathWriteBlocked`) so analysis's
   `except Exception` cannot swallow it, every refusal is recorded, and the runner **fails the
   run** if any was recorded, even if something swallowed it.
3. **Effective-settings proof** before any test or database exists: roots inside the
   namespace, ingest blank, Hour Refill unarmed, no production sockets, `DB_NAME` and
   `DB_USER` both `isadoraair_dev`, and the code is not the production checkout.

Protected roots: `/srv/isadoraair`, `/var/lib/isadoraair`, `/opt/isadoraair` (and the checkout it
resolves to), `/opt/isadoraair-runtime`, `/etc/isadoraair`, plus any path the production
environment file names for the six root settings.

### Entry points

| Entry point | Behaviour |
|---|---|
| `manage.py test ...` | `sys.argv[1] == 'test'` engages isolation in `settings.py`; `TEST_RUNNER` (`isadoraair.test_runner.IsolatedDiscoverRunner`) re-proves it before the database is created, prints the effective roots, and fails the run on any refused write. |
| `production/tests/run_with_validation_service.sh ...` | Creates the namespace, runs `manage.py test_isolation_preflight` first (nothing starts if it fails), then hands `ISADORAAIR_TEST_DATA_ROOT` to the transient validation service, the transient render service and the delegated test child, and removes the namespace on exit. |
| Anything else that loads a test package (`manage.py shell` + unittest, `python -m unittest`) | Every `<app>/tests/__init__.py` calls `test_safety.install()`, which now raises `UnsafeTestEnvironmentError` unless isolation is engaged. A test module therefore errors at import instead of running against station storage. |

### Writing tests

* A test that exercises `analyze_one_track`, `sync_track_file`, upload or reanalysis should
  override `WAVEFORMS_DIR` with its own scratch directory next to its `LIBRARY_ROOT`
  override, assert that `Track.waveform_path` is under it, and parse the JSON. Join any
  worker thread (and assert it finished) before the scratch directory is removed.
* A test that deliberately provokes a refusal registers a simulated station directory with
  `test_isolation.protect_for_test(path)` and calls `clear_blocked_writes()` in a cleanup.
  Never aim a provocation at a real `/srv` path.
* An `override_settings` of a root to a protected path is not rejected at override time (some
  tests read such a literal) but any write through it is refused by layer 2.

### Preflight (no test is run, no database is touched)

```bash
ISADORAAIR_TEST_DATA_ROOT=$(mktemp -d /tmp/isadoraair-test-data.XXXXXX) \
  ~/.venvs/isadoraair-dev/bin/python manage.py test_isolation_preflight
```

prints the effective roots, the database identity, which stale values were not honoured and the
result of eight non-creating probes against real station paths (all must be `refused`).

## Limits (read before trusting it)

* The audit hook sees Python-level operations in the processes that load these settings (the
  test process, and the guarded runner's validation/render services). It does **not** see a
  child process such as ffmpeg writing to a path it was given; those are covered by the root
  redirection, not by the hook. Hard links and bind mounts are not resolved.
* Isolation engages for `manage.py test`, the guarded runner and anything importing a test
  package. A bespoke script that imports test helpers without a test package, or runs
  production code directly, is outside it.
* Tests that open sockets to a real service are unchanged except that the production
  validation/render sockets are never used.
* A rare legitimate test write into a protected path will now fail loudly. That is intended;
  fix the test to use scratch.

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
