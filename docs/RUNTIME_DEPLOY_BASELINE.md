# Runtime deployment baseline — Runtime Foundation E6

Runtime Foundation E6 consolidates the deployment-baseline preflight
(`manage.py check_deploy_baseline`, IsadoraAir 1.2 Phase 3) and the
bare-machine restore procedure (`deploy/restore/`, Phase 4) onto the
completed Runtime Foundation E1–E5 architecture, so both answer one
coherent question: **is this host structurally capable of satisfying
the station's runtime contract, and can a restored/clean host establish
the required system surfaces without relying on historical one-off
checks?**

See `docs/RUNTIME_FOUNDATION_E.md` for how this fits into the whole E1–E8
sequence.

## Architecture

`isadoraair/deploy_baseline.py` is the single, reusable, read-only
aggregate entry point — `evaluate_deployment_baseline()`.
`monitoring/management/commands/check_deploy_baseline.py` is a thin
presentation layer over it: it contains no independent capability-
detection logic of its own for anything Runtime Foundation E already
owns.

```
evaluate_deployment_baseline()
  |
  +-- STRUCTURAL tier (evaluate_structural_baseline) -- no station DB needed
  |     |
  |     +-- legacy host/system checks (isadoraair.deploy_baseline.legacy_checks)
  |     +-- package prerequisite PRESENCE (isadoraair.runtime_packages)
  |     +-- Runtime Foundation E5 system surfaces (isadoraair.runtime_surfaces)
  |     +-- the pre-existing TTS scratch surface (isadoraair.runtime_scratch)
  |
  +-- LIVE/STATION tier -- canonical / only; needs a working station database
        |
        +-- PostgreSQL connectivity
        +-- Runtime Foundation E1/E2 runtime evidence
        |   (isadoraair.runtime_validation.validate_current_runtime)
        +-- package prerequisite REQUIRED-NESS, resolved against the
            same station selection E1/E2 already computed
```

## Legacy baseline consolidation

The original `check_deploy_baseline.py` (IsadoraAir 1.2 Phase 3) predates
Runtime Foundation E entirely and had its own independent fdkaac/Kokoro/
Piper detection logic using hard-coded, non-canonical paths
(`/home/jreed/kokoro`, `/home/jreed/weather-ingest/venv/bin/piper`, and
`encoders.services.encoder_manager.FDKAAC_PATH` validated via
`deploy/check_he_aac.sh` with **no** `--runtime-only` flag — i.e. always
requiring build-time pkg-config metadata). None of that independent
detection logic survives in E6.

| Check | Disposition |
|---|---|
| Python version | **Retained** — not Foundation-E-owned. |
| PostgreSQL client tools (`psql`/`pg_dump`/`pg_restore`) | **Retained.** |
| PostgreSQL connectivity | **Retained in the live/station tier only.** It is never executed by `--structural-only` or an offline `--target-root`. |
| GStreamer + every required element | **Retained** — not Foundation-E-owned. |
| Liquidsoap | **Retained.** |
| ALSA utils + snd-aloop layout | **Retained.** |
| `/opt/isadoraair` canonical app-root presence | **Retained** — established by `deploy/restore/20-application.sh`, not by Foundation E5 (E5's surfaces are the launcher/runtime-root/tts-asset-root/tmpfiles-config, never the application checkout itself). |
| Library root (`/srv/isadoraair/music`) | **Retained.** |
| `/run/isadoraair` (parent) presence | **Retained as-is** (shallow presence only) — `/run/isadoraair/tts` itself now gets the richer scratch-surface evidence below instead. |
| fdkaac (independent `check_he_aac.sh` invocation, pkg-config required) | **Replaced by Foundation E** — `isadoraair.runtime_validation.RuntimeValidator._validate_fdkaac`, which already calls the validator with `--runtime-only` (see "The fdkaac fix" below). |
| Kokoro (hard-coded `/home/jreed/kokoro`) | **Replaced by Foundation E** — E1/E2's own canonical `/opt/isadoraair-runtime/kokoro/venv` evidence. |
| Piper (hard-coded weather-ingest venv path) | **Replaced by Foundation E** — E1/E2's own canonical `/opt/isadoraair-runtime/piper/venv` evidence. |

Nothing was removed as merely redundant — every check above is either
retained verbatim or replaced by evidence Foundation E now owns
authoritatively.

## The fdkaac fix

`check_deploy_baseline.py` used to invoke
`encoders.services.encoder_manager.FDKAAC_PATH` +
`deploy/check_he_aac.sh` **without** `--runtime-only`. That validator, run
with no arguments, checks the *canonical production* paths and — with no
arguments — always requires build-time pkg-config metadata
(`PKG_CONFIG_PATH=$LIB_DIR/pkgconfig pkg-config --modversion fdk-aac`).
Runtime Foundation E4 deliberately publishes only the runtime-critical
canonical material — `/usr/local/bin/fdkaac` and
`/usr/local/lib/libfdk-aac.so.<version>` — and never
`lib/pkgconfig/fdk-aac.pc`. A healthy E4 canonical install would
therefore have been reported `MISSING`.

E6's baseline never runs that check independently. It calls
the public `isadoraair.runtime_validation.validate_current_runtime`
API, whose internal validator invokes
`deploy/check_he_aac.sh --fdkaac <binary> --lib-dir <library_root>
--runtime-only`. Python appends `--runtime-only` explicitly whenever it
passes a binary or library root; the shell does not infer it. Runtime
validation therefore requires `pkg-config` for absolutely nothing. This is the single authority now
consulted; `check_deploy_baseline.py` contains no independent fdkaac
capability logic of its own.

**Regression test:** `isadoraair/tests/test_deploy_baseline.py::
FdkaacFalseNegativeClosureTests` proves an E4-minimal canonical install
(binary + versioned library, no pkg-config metadata staged) evidences
`pass`, that a genuinely broken install evidences `fail`, and that the
build-only `BUILD_HEAAC` package group being entirely missing on a host
never gates the aggregate result once the runtime artifact itself is
healthy.

## Package prerequisite architecture

`isadoraair/runtime_packages.py` bridges `runtime_components.json` onto
the pre-existing Ubuntu package authority,
`deploy/packages-ubuntu-26.04.txt`, without ever duplicating package
membership into Python or a second JSON file.

**Component → package-authority-group relationship** (in
`runtime_components.json`):

```json
"components": {
  "kokoro": {
    "runtime": { "ubuntu_packages_group": "OPTIONAL_KOKORO_TTS", "...": "..." }
  },
  "fdkaac": {
    "build": { "ubuntu_packages_group": "BUILD_HEAAC", "...": "..." }
  }
}
```

The containing block (`runtime` vs. `build`) is what keeps a RUNTIME
prerequisite distinct from a BUILD-ONLY one — not a second invented
field name. `fdkaac.build.ubuntu_packages_group` already existed before
E6 (consumed by `deploy/build_fdkaac.sh`/`deploy/check_he_aac.sh`);
`kokoro.runtime.ubuntu_packages_group` is E6's one addition, following
that same precedent. Piper's `runtime` block deliberately has no such
key at all — it is self-contained (`docs/PIPER_PROVENANCE.md`) and must
never gain an invented apt requirement.

**Package membership stays authoritative in
`deploy/packages-ubuntu-26.04.txt`.** `isadoraair.runtime_packages.
parse_package_groups()` is a small, read-only parser recognizing
exactly that file's own `NAME=(\n  pkg\n  pkg\n)` bash-array shape — it
is not a bash interpreter, and it never hardcodes a package name. Blank
lines and comments are accepted; every other line must be a complete
group declaration, simple package token, or group close. Shell
statements, interpolation, trailing syntax, malformed arrays, duplicate
groups, and duplicate members are rejected. The runtime-component
loader validates every referenced group against this parsed authority,
so an unknown group invalidates the product contract immediately.

### Runtime vs. build package prerequisites

A **RUNTIME** package prerequisite (Kokoro → `OPTIONAL_KOKORO_TTS` →
`espeak-ng`) gates the aggregate baseline result when the owning
component is actually required by station configuration. A **BUILD**
package prerequisite (fdkaac → `BUILD_HEAAC` → autoconf/automake/
libtool/pkg-config/...) is evaluated and reported for informational
completeness only — it **never** gates the result. Build tooling only
matters while actually building fdkaac
(`deploy/restore/50-native-deps.sh` already does its own defensive
re-check for that); a healthy, already-built canonical runtime must
never be failed merely because autoconf isn't installed. See
`isadoraair.deploy_baseline.StructuralBaselineEvidence.result` /
`DeploymentBaselineEvidence.result`, which explicitly filter
`kind == "runtime"` before considering FAIL/UNRESOLVED package
evidence.

### Package prerequisite evidence and result semantics

For a component with a declared group, `evaluate_package_prerequisite`
checks each member package's installed state via `dpkg -s <pkg>` — the
exact convention `deploy/restore/10-packages.sh` already uses (never
`command -v`, which would conflate "a binary happens to be on PATH"
with "the package is actually installed"). No package installation ever
occurs in read-only validation. Only the trusted absolute Ubuntu paths
`/usr/bin/dpkg` and `/bin/dpkg` are considered; caller `PATH` is never
used. A missing executable, timeout, or unexpected execution result is
reported as `unresolved`, not misreported as an absent package.

| `required` | packages present | status |
|---|---|---|
| `True` | all present | `pass` |
| `True` | any missing | `fail` |
| `False` | all present | `pass` |
| `False` | any missing | `optional_absent` (not a station failure) |
| `None` (unresolved) | either | `unresolved` (never a guessed `pass`) |
| no group declared for this component/kind | n/a | `not_applicable` |

## Structural vs. station baseline

**Structural tier** — evaluable with no station database at all:
manifest validity, legacy host/system checks, package prerequisite
evidence on live `/`, the E5 system surfaces, and the scratch surface.
`manage.py check_deploy_baseline --structural-only` reports only this
tier — useful for a pre-database installer/bootstrap context. It never
opens a database connection or attempts a station-selection query.

`--target-root /mnt/restored` maps canonical filesystem evidence beneath
that offline root and automatically suppresses all live/station checks.
It validates target files and the combined package contract, but does
not execute target runtimes, inspect installer-host kernel/services, or
borrow installer-host dpkg state. Persistent launcher/tmpfiles content
is still compared with eventual boot-root canonical paths; the mount
prefix is never embedded.

**Station tier** — requires a working station database/configuration:
which optional runtimes (Kokoro/Piper/fdkaac) are actually required, and
whether they pass Foundation E1/E2's own component validation. Reuses
`isadoraair.runtime_validation.validate_current_runtime`, which already
fails closed (`requirement_errors` populated, never a guessed healthy
result) when the database can't be inspected — E6 does not reimplement
this, it consumes it. Package-prerequisite `required`-ness for this tier
comes from the very same `ComponentRequirement.required` E1/E2 already
resolved, and is `None` (→ `unresolved`) whenever the station itself
was unresolved.

## Scratch-surface (`/run/isadoraair/tts`) evidence

`isadoraair/runtime_scratch.py` gives read-only evidence for the
pre-existing, service-account-owned TTS scratch surface. It is
deliberately **not** added to Runtime Foundation E5's own root-owned
tmpfiles config (`deploy/isadoraair-runtime-tmpfiles.conf` excludes
it) — it stays owned by the pre-existing
`deploy/isadoraair-tmpfiles.conf` / `@@ISA_USER@@` convention
`deploy/restore/90-system-config.sh` already installs. E6 only
*validates* that existing authority's own surface; it never becomes a
second establishing mechanism for it.

Service identity is never guessed. On live `/`, the caller must supply the same
`ISA_USER` `deploy/restore/90-system-config.sh` itself resolves (`id
-un`, or an explicit override) via `--isa-user`. Without it, evidence is
`unresolved_identity` — explicitly distinct from, and never conflated
with, a healthy directory. `deploy/restore/95-validate.sh` now resolves
`ISA_USER` the same way `90-system-config.sh` does and threads it
through automatically, so a real post-restore run resolves this
meaningfully rather than reporting it unresolved on every invocation.
For an offline target, the same name is resolved only from
`<target-root>/etc/passwd`, never from a same-named installer-host
account. Trusted tooling/tests may instead supply an explicit numeric
UID/GID pair. Validation rejects a symlink or non-directory anywhere in
the confined scratch ancestry before inspecting the final `0700`
directory.

## Runtime Foundation E5 restore integration

**Fixed destination mapping** (`deploy/restore/90-system-config.sh`):

| Source | Destination |
|---|---|
| `deploy/isadoraair-tmpfiles.conf` (pre-existing) | `/etc/tmpfiles.d/isadoraair.conf` (unchanged) |
| `deploy/isadoraair-runtime-tmpfiles.conf` (Runtime Foundation E5) | `/etc/tmpfiles.d/isadoraair-runtime.conf` (was previously falling through the generic loop to the wrong `systemd/system/<basename>` destination with unsubstituted `@@ISADORAAIR_SURFACE_UID@@`/`@@ISADORAAIR_SURFACE_GID@@` markers — now excluded from that generic loop and given its own dedicated step) |

**Establishment layer.** By the time stage 90 runs, `20-application.sh`
(clone) and `60-python.sh` (venv) have already run, so the venv +
application checkout are available. `90-system-config.sh` therefore
*prefers* invoking Runtime Foundation E5's own reusable API —
`manage.py provision_runtime_components --surfaces --apply
--target-root <staging-root-or-/>` — rather than developing a second
mkdir/chown/chmod-and-render-tmpfiles implementation. This establishes
all four E5 surfaces (launcher, `runtime_root`, `tts_asset_root`,
`tmpfiles_config`) through the exact same renderer `apply()` and
`validate()` both already use, so nothing restore installs can ever
disagree with what E5's own validation later expects.

If the venv/checkout are not yet available at this point (e.g. this
stage re-run in isolation before `60-python.sh`), it falls back to a
minimal direct render of `deploy/isadoraair-runtime-tmpfiles.conf`
only (substituting `@@ISADORAAIR_SURFACE_UID@@`/`@@ISADORAAIR_SURFACE_GID@@`
with `0`/`0` on a real host or the caller's own numeric uid/gid under
`--staging-root`) — file only, no directories, no real
`systemd-tmpfiles` execution. Later validation (`check_deploy_baseline`,
or re-running this stage once the venv exists) is what converges the
rest through E5's own authority; this fallback never becomes a second,
competing establishing mechanism. It is explicitly incomplete: stage 95
will fail while the E5 launcher/directories are absent, so fallback can
never produce final restore success.

**Target-root correctness.** `90-system-config.sh`'s own
`E5_TARGET_ROOT` mirrors its existing `ETC_ROOT` staging/real-host
duality exactly (`$RESTORE_STAGING_ROOT` or `/`), and Runtime Foundation
E5's own rendering contract (established in E5, unmodified here) keeps
"where a file is written" and "what a launcher's own content refers to"
strictly separate: a staging-root install still embeds the canonical
`/opt/isadoraair`, never the installer's own mount prefix. See
`isadoraair/tests/test_restore_tooling.py::
RuntimeFoundationE5SystemConfigFunctionalTests` for a real,
disposable-root proof of both the destination mapping and this content
guarantee.

**Stage-95 target validation.** A canonical `/` run performs Django,
live PostgreSQL/station runtime, migration-plan, and structural checks.
A `--staging-root` run instead invokes
`check_deploy_baseline --structural-only --target-root <staging-root>`.
It validates the mounted target's launcher, E5 directories, both
tmpfiles configs, application/library paths, scratch ancestry and target
identity without depending on installer-host services. This is an
offline filesystem acceptance tier, not a claim that the target's live
database, kernel, or TTS/native runtimes have run before boot.

**Both tmpfiles authorities remain distinct on purpose** — see
`deploy/isadoraair-runtime-tmpfiles.conf`'s own header comment. They are
never merged for convenience; `systemd-tmpfiles` processes every
`*.conf` under `tmpfiles.d/` independently, so both coexist without
coordination.

## No network fallback

Nothing in this consolidation ever fetches package metadata, a wheel, a
model, or a native-source archive over the network. Package-prerequisite
evidence is read-only `dpkg -s` state; if a required package group is
missing, baseline reports it as an actionable failure for an operator
(or `deploy/restore/10-packages.sh`, which already installs named
groups from the same authority file via `--with-kokoro-tts` etc.) to
address explicitly — never a silent fetch.

## Remaining E7/E8 responsibilities

- **Update, Runtime Foundation E7B (2026-08-29):** for backup-based
  disaster recovery specifically, `deploy/restore/50-native-deps.sh`
  and `70-tts.sh` now delegate to Foundation E4/E3's own canonical
  provisioners via the embedded recovery payload — see
  `docs/RUNTIME_BACKUP_PAYLOAD.md`'s "Restore integration" section. The
  pre-Foundation-E mechanisms described below (a throwaway
  `native/fdkaac` prefix, `$HOME/kokoro`/`$HOME/piper`) remain, but only
  as an explicit, separately-invoked connected/fresh-install mode —
  never a backup-based restore's default path.
- E7 also owns actually shipping the wheel/model/native-source bundles
  E3/E4's provisioners consume.
- E8 owns fully offline, disposable, whole-machine acceptance once E7's
  payload consumption exists.
