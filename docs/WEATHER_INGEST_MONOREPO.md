# weather-ingest monorepo migration (r0048)

Roadmap item P0/1.2 follow-on. `weather-ingest` -- previously a
standalone, privately-versioned companion project -- is now imported
in-tree at `weather_ingest/`. This document is the provenance record
and the current architecture reference; see "What changed" below for
exactly what did and did not change in this migration.

## Provenance

| Field | Value |
|---|---|
| Source repository | `celltech161/weather-ingest` (private) |
| Source tag | `weather-ingest-standalone-final-2026-09-08` |
| Source commit SHA | `ea755f2c6092da293028fd60a9178e53171cadff` |
| Import method | `git archive <tag> \| tar -x` (exact tracked tree only -- no `.git`, no history, no untracked state) into `weather_ingest/` |
| Import date | 2026-09-08 |
| Byte-identity | Verified via per-file SHA-256 against the source tag before any migration-specific edit (see the import's own commit, which contains ONLY the unmodified tree) |
| Canonicalization commits already represented in the imported history (confirmed present as ancestors of the source SHA before import; not separately re-applied here) | `7e0dab1 Use Django WEATHER_DATA_DIR across weather jobs`, `ea755f2 Make weather tests independent of legacy data` |

The standalone repository, its final tag, the temporary deploy key used
to fetch it, and the retired `/home/jreed/weather-ingest` checkout
itself are all deliberately preserved as rollback/historical material
for now -- see "Rollback" below. This document does not mean any of
that material has been deleted.

`weather-ingest`'s own Git history is NOT grafted into IsadoraAir's --
the final standalone tag is the historical boundary. Its own commit
history remains fully available in the private `celltech161/weather-ingest`
repository for as long as that repository is retained.

## What changed

**Nothing in the imported Python source.** Every entry point already
resolved its own location-independent inputs through environment
variables with production-correct defaults (`ISADORAAIR_DIR`, defaulting
to `/opt/isadoraair`; `LIBRARY_ROOT`, defaulting to
`/srv/isadoraair/music`; the canonical `/usr/local/bin/isadoraair-tts`
CLI path) -- none of them hardcode their OWN checkout location or venv
path anywhere, so moving the checkout from `/home/jreed/weather-ingest`
to `/opt/isadoraair/weather_ingest` required zero source edits. See
`weather_ingest/lib/wxconfig.py` and `weather_ingest/lib/delivery.py`
for these defaults.

**What did change** is entirely in IsadoraAir's own deployment/recovery
tooling, never in the imported project itself:

- `deploy/restore/60-python.sh` -- builds the isolated
  `weather_ingest/venv` (from `weather_ingest/requirements.txt`) for a
  modern target, alongside the existing main venv.
- `deploy/restore/80-companions.sh` -- a modern target's default
  companion set is now `syndicated-ingest` + `ogremote-ingest` only
  (weather is in-tree, no longer a separate clone). A legacy target
  (pre-monorepo IsadoraAir revision) still provisions the standalone
  `weather-ingest` companion exactly as before.
- `deploy/restore/90-system-config.sh` -- renders `@@WEATHER_ROOT@@` to
  the in-tree root for a modern target, the legacy companion root for a
  legacy target.
- `deploy/updater-station.example.json` -- the canonical modern example
  now shows `"weather_root": "/opt/isadoraair/weather_ingest"`.
- `deploy/wx-*.service` templates are deliberately **byte-unchanged** in
  this release. They already used `@@WEATHER_ROOT@@` for both
  `WorkingDirectory` and `ExecStart` and need no edit at all to resolve
  to the in-tree root once `render_values.weather_root` is updated.
  `Environment=ISADORAAIR_DIR=@@ISA_ROOT@@` is added directly to the
  live installed units as one deliberate, manual step during the
  production cutover instead (see "Production cutover" below) -- NOT
  as a tracked template change in this git release. Reason: on this
  production host, these 7 `.service` units (and their paired `.timer`
  units, and the separate, not-yet-git-tracked `amber-alert-poll.service`/
  `.timer`) are installed and maintained OUTSIDE Update Center's own
  managed-unit governance -- confirmed by inspection: no active signed
  protected-policy document exists on this host
  (`/usr/local/bin/isadoraair-updater`'s entry root has no
  `protected-policy.json`), and none of these unit names appear in
  `deploy/updater_runtime/isadoraair_updater/release.py`'s compiled
  `MANAGED_UNIT_POLICIES`. An ordinary (non-protected-runtime) release
  manifest's own `systemd_units_changed` is cross-checked against the
  REAL git diff of `deploy/*.service`/`*.timer` between commits
  (`release.py`'s `_verify_manifest_matches_diff`-equivalent check) and
  then, separately, against that compiled managed-unit set
  (`manual_blockers`'s `UNKNOWN_MANAGED_UNIT`) -- so committing a
  content change to these specific files in an ordinary release would
  make the release itself fail to deploy through Update Center at all,
  unless these unit names were first added to `MANAGED_UNIT_POLICIES`.
  That addition is explicitly out of scope here (the module's own
  comment calls it "a real, reviewable protected-runtime code change,"
  and this migration must not be a protected-runtime change) -- so the
  checked-in templates stay untouched, and the actual production units
  are updated by hand, matching exactly how they are already
  maintained on this host today. A future, separate pass may choose to
  formally onboard these units into Update Center's governance; that is
  intentionally deferred, not done here.

**What deliberately did NOT change**: the `weather_root` /
`@@WEATHER_ROOT@@` updater render-key contract itself (still required,
still validated the same way -- see
`deploy/updater_runtime/isadoraair_updater/config.py`'s `_RENDER_KEYS`);
the actual production `render_values.weather_root` value (changed only
as an explicit, separate, root-owned station-configuration edit AFTER
direct on-host validation -- see "Production cutover" below, not as
part of any code release); weather-ingest's own dependency versions,
runtime behavior, or venv isolation semantics (`python3 -m venv`, no
`--system-site-packages` -- confirmed unnecessary by inspecting the
imported `requirements.txt`/`README.md`, which already state this
project has no GStreamer/PyGObject dependency).

## Ownership boundaries

| Path | Owns |
|---|---|
| `/opt/isadoraair/weather_ingest` | Git-owned executable source (this import) |
| `/opt/isadoraair/weather_ingest/venv` | Rebuildable, `.gitignore`d weather runtime venv (isolated from IsadoraAir's own main venv) |
| `/var/lib/isadoraair/weather` | Mutable weather state/data (unchanged, already canonical since r0047) |
| IsadoraAir/Django/PostgreSQL | Authoritative weather configuration (unchanged, already authoritative since r0047 via `dump_weather_config`) |

## Modern vs. legacy restore targets

A **modern** target is any checked-out IsadoraAir revision that
contains `weather_ingest/requirements.txt` (this import, r0048+). A
**legacy** target is any revision that predates it (r0047 and earlier).
The restore tooling detects this structurally (file presence), never by
release-number comparison, so a future release doesn't need to keep
extending a hardcoded list. See `deploy/restore/lib.sh`'s
`restore_target_has_intree_weather` and each stage's own header
comment for the exact contract. This exists so an OLDER backup archive
(taken before this migration) can still be restored using the
standalone `weather-ingest` companion path -- this migration does not
remove that ability.

## Rebuilding the weather venv

```bash
cd /opt/isadoraair/weather_ingest
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

No `--system-site-packages` (unlike the main IsadoraAir venv) --
`requests` is this project's only direct PyPI dependency.

## Production cutover

This release (r0048) only imports source and updates recovery/restore
tooling -- it does **not** repoint any production weather timer. The
cutover to the in-tree runtime is a separate, later, manual sequence,
performed only after r0048 is installed and the in-tree venv has been
built and directly validated on production:

1. Build `/opt/isadoraair/weather_ingest/venv` on production (see
   above) and run its test suite / a safe read-only weather job (e.g.
   `update_local_wx_data.py`) directly, outside systemd, to prove the
   new runtime works end to end against the real `WeatherConfig`.
2. Take a root-protected copy of the authoritative station
   configuration (`/etc/isadoraair/station.json`) before changing
   anything.
3. Atomically edit ONLY `render_values.weather_root` in that file to
   `/opt/isadoraair/weather_ingest`, and validate the edited file
   through the existing protected updater/worker config validator.
4. Directly edit the 7 live `wx-*.service` units (and, for
   consistency, `amber-alert-poll.service`, which shares the same
   checkout/venv) under `/etc/systemd/system/` to point at the new
   root and add `Environment=ISADORAAIR_DIR=/opt/isadoraair` -- the
   same manual mechanism these specific units are already maintained
   through today (see "What changed" above for why this is not a
   git-tracked template change). `systemctl daemon-reload` afterward.
5. Briefly stop only the timers that can launch an affected unit
   immediately before this edit, to avoid a job firing mid-cutover;
   restore their exact prior enabled/active state afterward.

Rollback: restore the saved station-configuration copy (`weather_root`
back to `/home/jreed/weather-ingest`) and revert the 7 unit files (+
`amber-alert-poll.service`) to their pre-cutover content, then
`systemctl daemon-reload`. `/home/jreed/weather-ingest` is retained
precisely so this rollback path stays available -- see "Rollback"
below.

## Rollback

Until this migration's own subsequent backup/recovery evidence is
accepted, `/home/jreed/weather-ingest` remains in place, clean, and at
the exact final standalone commit -- untouched by this migration. See
`docs/DISASTER_RECOVERY_STATUS.md` for the exact rollback procedure
used for the production systemd-path cutover (station config
`render_values.weather_root` back to `/home/jreed/weather-ingest`) and
its current status.
