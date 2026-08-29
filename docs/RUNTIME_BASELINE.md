# Runtime baseline — IsadoraAir 1.2 Phase 3

Central reference for the reproducible dependency baseline established
by Phase 3 (2026-08-12). Individual subjects have their own focused
docs (linked below); this file ties them together: the supported
platform definition, restore ordering, the preflight command, and the
generic-vs-station-specific boundary every other Phase 3 document
follows.

## Supported runtime baseline

```
Ubuntu 26.04 LTS
Python 3.14 as provided by Ubuntu 26.04
PostgreSQL 18
GStreamer 1.28.x, with 1.28.6 decision deferred to roadmap item 1.4
Liquidsoap 2.4.x
```

| Component | Production version | Pin policy | Why |
|---|---|---|---|
| Ubuntu | 26.04 LTS (Resolute Raccoon) | **Exact-pinned** | The LTS release itself is the reproducibility anchor everything else is defined relative to. |
| Python | 3.14.4 (`python3.14` package `3.14.4-1ubuntu0.1`) | **Minimum-supported (3.14), OS-managed patch** | Ubuntu 26.04 ships 3.14 as `python3`; no reason to pin an exact patch release — security updates should keep flowing. |
| PostgreSQL | 18.4 (`postgresql-18` package) | **Major-pinned (18), OS-managed patch** | Schema/migrations verified against PG 18 specifically; patch releases are OS-managed. |
| GStreamer | 1.28.2 (`gstreamer1.0-tools` etc.) | **Minor-series-pinned (1.28.x)**, exact patch (1.28.2) is current production; **1.28.6 upgrade decision explicitly deferred to roadmap 1.4** | See "GStreamer 1.28.6 boundary" below. |
| Liquidsoap | 2.4.0+dev (`liquidsoap` package `2.4.0-1build9`, Ubuntu universe) | **Minor-series-pinned (2.4.x)** | Ordinary apt package, no custom build; `+dev` suffix is Ubuntu's own packaging label, not evidence of a non-standard build (confirmed via `dpkg -l`/`apt-cache policy` — genuinely from `resolute/universe`, not OPAM or source). |

**No ordinary Ubuntu package is exact-pinned beyond what's shown above**
— over-pinning routine dependencies (nginx, ALSA utils, ffmpeg,
build-essential, etc.) would make the installer brittle for no benefit;
they track whatever Ubuntu 26.04 ships/patches, which is the intended
"OS-managed/current-for-release" behavior. The two deliberate
exceptions (fdk-aac/fdkaac, Kokoro/Piper model artifacts) are pinned by
upstream tag or checksum instead, documented in their own files below
— they're not Ubuntu packages at all.

## Document map

| Subject | Document |
|---|---|
| HE-AAC/fdkaac build + validation | `deploy/build_fdkaac.sh`, `deploy/check_he_aac.sh`, `docs/HE_AAC_FDKAAC_PROVENANCE.md` |
| GStreamer element inventory | `docs/GSTREAMER_ELEMENT_INVENTORY.md` |
| Companion-project dependency manifests | `syndicated-ingest/requirements.txt`, `weather-ingest/requirements.txt`, `ogremote-ingest/requirements.txt` (each project's own README "Dependencies"/"Runtime integration prerequisites" section) |
| Hardcoded path audit + canonical path | `docs/HARDCODED_PATH_AUDIT.md` |
| ALSA/device inventory + snd-aloop | `docs/ALSA_DEVICE_INVENTORY.md`, `deploy/isadoraair-aloop.conf` |
| Kokoro TTS | `docs/KOKORO_PROVENANCE.md` |
| Piper TTS | `docs/PIPER_PROVENANCE.md` |
| TTS support matrix + boundaries | `docs/TTS_SUPPORT_MATRIX.md` |
| Runtime component machine contract | `isadoraair/runtime_components.json`, `docs/RUNTIME_COMPONENTS.md` |
| PostgreSQL baseline | `docs/DISASTER_RECOVERY.md`'s "Database restore" section |
| System package manifest | `deploy/packages-ubuntu-26.04.txt` |
| Preflight checker, consolidated onto Runtime Foundation E (E6) | `monitoring/management/commands/check_deploy_baseline.py` (`manage.py check_deploy_baseline`), `isadoraair/deploy_baseline.py`, `docs/RUNTIME_DEPLOY_BASELINE.md` |
| Runtime Foundation E phase index | `docs/RUNTIME_FOUNDATION_E.md` |
| This file | Restore ordering, generic/station-specific boundary, supported-baseline summary |

## GStreamer 1.28.6 boundary (roadmap item 1.4)

This phase deliberately does **not** perform the 1.28.2 → 1.28.6
upgrade — that's roadmap item 1.4's own scope. What this phase
establishes so 1.4 doesn't have to re-derive it:
- **Current production**: 1.28.2, all from Ubuntu 26.04's own repos
  (`gstreamer1.0-*` packages, no PPA/custom build).
- **Required elements/packages**: the full element-by-element mapping
  in `docs/GSTREAMER_ELEMENT_INVENTORY.md` — every package name listed
  there is what 1.4 needs to re-verify still supplies the same
  elements after any version bump, not re-discover from scratch.
- **Reproducible installation source on Ubuntu 26.04 today**: apt, from
  the packages in `docs/GSTREAMER_ELEMENT_INVENTORY.md`/
  `deploy/packages-ubuntu-26.04.txt`'s `AUDIO_GSTREAMER` array — 1.28.2
  is what that repo currently offers.
- **What changes if 1.4 moves to 1.28.6**: most likely a PPA, a
  backport repo, or a source build, since 1.28.6 is not (as of this
  writing) what Ubuntu 26.04's own archive ships. Whichever mechanism
  1.4 chooses, `docs/GSTREAMER_ELEMENT_INVENTORY.md`'s element table
  and `check_deploy_baseline`'s element-presence checks should need no
  changes — they check element availability, not the package's exact
  provenance, so they keep working as the validation gate for whatever
  1.4 installs.

## Restore-order dependency map

```
OS packages (deploy/packages-ubuntu-26.04.txt)
  ↓
PostgreSQL (role + database created, see docs/DISASTER_RECOVERY.md)
  ↓
IsadoraAir checkout at /opt/isadoraair + .env
  ↓
Python venv (--system-site-packages; requirements.txt)
  ↓
Database restore (pg_restore)
  ↓
/srv/isadoraair storage mount (717+ GB music library — separate
  storage-resilience concern, see docs/DISASTER_RECOVERY.md)
  ↓
Native dependencies: fdkaac/libfdk-aac (deploy/build_fdkaac.sh),
  snd-aloop layout (deploy/isadoraair-aloop.conf), Kokoro, Piper
  ↓
Companion repos (syndicated-ingest, weather-ingest, ogremote-ingest --
  each with its own venv + requirements.txt)
  ↓
systemd units (deploy/*.service, *.timer)
  ↓
Services started
```

**Why this order matters, not just what the steps are:**
- PostgreSQL must exist and be reachable *before* `manage.py` can do
  anything — including the `dump_weather_config`/`dump_ogremote_config`/
  `send_weather_notification`/`send_ogremote_notification` commands the
  companion projects shell out to.
- **weather-ingest and ogremote-ingest cannot function until
  IsadoraAir's own database and management commands exist** — both
  projects' `lib/wxconfig.py`/`lib/config.py`/`lib/notify.py` are
  cross-venv subprocess calls into `$ISADORAAIR_DIR/venv/bin/python
  manage.py <command>`. Standing up either companion project before
  IsadoraAir itself is migrated and running is pointless — every
  config read and every failure notification would fail immediately.
  syndicated-ingest is the one companion project that does **not**
  have this dependency for its core fetch/deliver path (its own
  `lib/delivery.py` calls `sync_track_file` the same way, so it too
  needs IsadoraAir's venv+DB — the distinction is that weather-ingest
  and ogremote-ingest additionally depend on IsadoraAir for their own
  *configuration*, not just delivery).
- fdkaac/Kokoro/Piper/snd-aloop can be built/installed any time before
  the services that need them start, but logically sit "after" the
  application checkout since none of them are Python-venv dependencies
  — they're native binaries/kernel modules the venv's own code shells
  out to.
- Run `manage.py check_deploy_baseline` after the native-dependencies
  step and again after companion repos are in place — it validates
  most of what's above in one read-only pass (see its own docstring
  for exactly what it does and doesn't check).

## Generic vs. station-specific boundary

Kept explicit throughout every Phase 3 document — worth restating here
as the single reference point.

**Generic / repo-managed** (a fresh IsadoraAir installer must not
hardcode Oak Grove specifics into any of these):
- `deploy/packages-ubuntu-26.04.txt` — Ubuntu package list.
- `deploy/build_fdkaac.sh` + `deploy/check_he_aac.sh` — HE-AAC build/validation.
- `docs/GSTREAMER_ELEMENT_INVENTORY.md` — required-elements list.
- `requirements.txt` (IsadoraAir + all 3 companion projects) — Python dependencies.
- `monitoring/management/commands/check_deploy_baseline.py` — generic preflight.
- The canonical `/opt/isadoraair` convention (`docs/HARDCODED_PATH_AUDIT.md`).
- `docs/KOKORO_PROVENANCE.md` / `docs/PIPER_PROVENANCE.md` — TTS engine
  *installation* recipes (not which voices to select).
- `isadoraair/runtime_components.json` — machine-readable runtime versions,
  artifact identities, canonical paths, and conditional/optional semantics.

**Station-specific** (Oak Grove's own configuration/hardware, never
baked into the generic tooling above):
- The physical sound-card mapping (`docs/ALSA_DEVICE_INVENTORY.md`'s
  `plughw:2,0` → onboard HDA codec assignment) — a different box's
  hardware will enumerate differently.
- The current `/srv` disk UUID and mount configuration.
- `.env` (all of it — secrets, station identity, feature toggles).
- StereoTool (its own binary, license, and saved processing profile).
- Oak Grove's specific companion-project configuration (`WeatherConfig`/
  `AmberAlertConfig`/`OGRemoteConfig` DB rows, `~/.syndicated_ingest.cred`
  etc.).
- Which TTS voices are actually selected
  (`weather-ingest/lib/voices.py`'s `VOICES` dict) — the engines
  themselves are generic, the choice of `af_jessica` for day and
  `am_fenrir` for night is not.

## Validation summary

See the Phase 3 completion report for the full command-by-command
record. Headline results: HE-AAC staged build byte-identical output to
production for a representative encode; `check_deploy_baseline` PASSes
every check on production and correctly reports OPTIONAL/MISSING/exit
codes under simulated-missing-dependency conditions; all three
companion-project dependency manifests validated via a from-scratch
venv rebuild + real synthesis/config-read smoke tests, not just syntax
checks.
