# Runtime Foundation E5: stable OS filesystem/CLI surfaces

Runtime Foundation is being built in numbered passes, each with a narrow,
independently reviewable scope:

```text
E1/E2  contract + station-requirement resolution + read-only validation
E3     offline Kokoro/Piper runtime preparation/publication
E4     native fdkaac preparation/publication
E5     stable OS filesystem/CLI surfaces                    <- this document
E6     baseline + restore consolidation
E7     backup/installer consumption
E8     offline disposable-machine acceptance
```

E5 makes four durable, product-owned system surfaces exist and be
correctly owned -- nothing more. It does not provision Kokoro, Piper, or
fdkaac (E3/E4 own that), does not migrate any historical caller onto the
new stable launcher, and does not activate anything in production.

## The four surfaces

| Surface | Canonical path | Owner |
| --- | --- | --- |
| Installed TTS launcher | `/usr/local/bin/isadoraair-tts` | root, `0755`, not service-writable |
| Provider runtime root | `/opt/isadoraair-runtime` | root, `0755`, not service-writable |
| TTS asset/data root | `/var/lib/isadoraair/tts` | root, `0755`, not service-writable |
| Runtime tmpfiles config | `/etc/tmpfiles.d/isadoraair-runtime.conf` | root, `0644` |

Every path above comes from `isadoraair/runtime_components.json`'s
`canonical_paths` -- the same single path authority Foundation E1-E4
already use -- via the existing, target-root-aware
`isadoraair.runtime_provisioning.ProvisioningLayout` (extended with
`application_root`, `tts_cli`, `tts_scratch` fields for E5, reusing the
same `_map()` mechanism E3/E4 already rely on). No path is duplicated as
an independent Python, shell, or test constant.

`/opt/isadoraair` (the application root) and `/run/isadoraair/tts`
(volatile TTS scratch) are also part of the canonical contract but are
**not** newly owned by E5:

- `/opt/isadoraair` is the existing production checkout root; E5 only
  *reads* it (to render the installed launcher's embedded path) and
  never creates, moves, or repairs it.
- `/run/isadoraair` and its `/tts` scratch subdirectory are already
  owned by the pre-existing `deploy/isadoraair-tmpfiles.conf`
  (`0700`, `@@ISA_USER@@`-owned) -- see "Why `/run/isadoraair/tts` is
  untouched" below.

## Inventory: who actually reads/writes each directory

Before choosing ownership, the actual E1-E4 code was inspected, not
assumed:

- **`/opt/isadoraair-runtime`**: Foundation E3 (`RuntimeProvisioner`)
  and E4 (`NativeRuntimeProvisioner`) are the only writers, and both
  already run privileged (root for canonical `/`, caller-owned for a
  mapped `--target-root`) and already publish everything beneath it at
  `0644`/`0755`, root-owned, via atomic same-directory-temp-then-replace.
  Nothing in `isadoraair/tts/*` or any TTS synthesis code path writes
  here directly -- callers only ever read the published `venv`/asset
  symlink pointers. **Conclusion: root-managed, service read/execute
  only, matching E3/E4's own existing convention exactly.**
- **`/var/lib/isadoraair/tts`**: identical reasoning -- E3 is the only
  writer (Kokoro/Piper asset generations), same publication discipline.
  **Root-managed, service read only.**
- **`/run/isadoraair/tts`**: grepped the entire Python tree for
  `tts_scratch` and this literal path -- **zero code consumers** exist
  today (confirmed against `isadoraair/tts/service.py`,
  `isadoraair/tts/kokoro.py`, `isadoraair/tts/providers.py`: the actual
  synthesis scratch file is created via `tempfile.mkstemp(dir=<final
  destination's own parent>)`, never under `/run`). It is, however,
  **already** an established, already-shipped production surface: the
  pre-existing `deploy/isadoraair-tmpfiles.conf` already creates it at
  `0700`, owned by `@@ISA_USER@@` (the real service account, substituted
  by the existing `deploy/restore/90-system-config.sh` render loop) --
  clearly a deliberate prior decision reserving it for a future TTS
  runtime-coordination writer under the *service* identity, not root.
  **Decision: leave this pre-existing entry completely untouched.**
  Per the guidance that governed this pass ("if service-user ownership
  cannot yet be derived authoritatively and the directory has no
  current writer, prefer a safe root-managed directory and document
  what future activation must resolve") -- ownership here is not
  invented by E5 at all; it was already decided before E5 and already
  matches the safe, narrow, service-account-private (`0700`) shape.
  E5 introduces no new writer and no new authority for this path.

This is why E5 adds a **second**, separate `deploy/isadoraair-runtime-
tmpfiles.conf` rather than editing the existing
`deploy/isadoraair-tmpfiles.conf` -- the two persistent, root-owned
directories E3/E4 actually need are a genuinely different ownership
model from the pre-existing, service-account-owned volatile scratch
directory, and `systemd-tmpfiles` processes every `*.conf` file under
`tmpfiles.d/` independently, so two files coexist without any
coordination logic. See "Tmpfiles authority" below.

## The two TTS launchers

**`deploy/isadoraair-tts`** (no suffix) is the pre-existing, **unchanged**,
Git-owned repo-local development launcher. It discovers `<checkout>/venv/
bin/python` relative to its own file location and works from any checkout.
E5 does not touch this file, its behavior, or its existing tests.

**`deploy/isadoraair-tts-canonical`** (new, E5) is a template, not a
runnable file on its own -- it contains one literal marker,
`@@ISADORAAIR_APPLICATION_ROOT@@`, substituted by
`isadoraair.runtime_surfaces.RuntimeSystemSurfaceManager` at publish
time, then written beneath `--target-root` to `/usr/local/bin/
isadoraair-tts`. The rendered, installed file is a fixed standalone
script -- unlike the repo-local launcher, it cannot discover its own
checkout, because once installed it no longer lives inside one.

`--target-root` maps **where** that file is written; it deliberately
never maps **what path the file's own content refers to**. By default
the marker is always substituted with the canonical, unmapped
`canonical_paths.application_root` from `runtime_components.json` --
`/opt/isadoraair` -- regardless of `--target-root`, because the
launcher's content is read only after the target filesystem is
actually running as `/` (an offline/restore target written beneath a
staging mount today boots as the real root later; the installer host's
own mount point would be meaningless there). `RuntimeSystemSurfaceManager`
exposes one explicit, narrowly-scoped opt-in,
`embed_mapped_application_root=True`, that substitutes the
target-root-mapped path instead; it is never inferred merely from
`--target-root` being non-canonical, and it is not exposed as an
ordinary operator CLI flag. Its only legitimate use is a disposable
test seam that needs the installed launcher to genuinely execute
against a fake application root beneath the same scratch directory --
see `isadoraair/tests/test_tts_installed_launcher.py`. Validation
(`current_evidence()`/`validate_system_surfaces()`) renders expected
launcher content through this same method, so apply and validate can
never disagree about which content is correct.

Both launchers share the identical logical-TTS execution chain:

```text
/usr/local/bin/isadoraair-tts (installed)  or  deploy/isadoraair-tts (repo-local)
        |
        v
canonical/checkout application Python, -E isolated
        |
        v
python -E -m isadoraair.tts
        |
        v
logical voice/provider selection (isadoraair.tts's own existing contract)
        |
        +-> Kokoro canonical runtime
        |
        +-> Piper canonical runtime
```

Neither launcher ever names a Kokoro/Piper runtime path directly --
provider selection is owned exclusively by `isadoraair.tts`
(`isadoraair/tts/cli.py`'s existing public logical-voice-only contract,
untouched by E5), reached only via `-m`.

The installed launcher additionally, deliberately:

- fails with one clear stderr line and exit 1 if the canonical
  application Python is missing -- never falls back to a `PATH` Python,
  never falls back to the repo-local launcher, never discovers
  `/home/jreed` or any other historical path;
- strips `PYTHONPATH` from the inherited environment;
- `os.chdir`s to the canonical application root before `exec`;
- uses `os.execve` (never spawn-and-proxy, never a shell) so argv and
  exit status pass through exactly.

## Tmpfiles authority

Ubuntu 26.04's `systemd-tmpfiles` (confirmed present at
`/usr/bin/systemd-tmpfiles`, and confirmed here to support both `--root=`
for isolated target-root operation and idempotent, non-recursive `d`-type
directory existence/mode/ownership repair) is the declarative, final
system-surface authority for `/opt/isadoraair-runtime` and
`/var/lib/isadoraair/tts`'s required ownership/mode -- no custom boot
service exists for either directory; `systemd-tmpfiles` already solves
that completely, including safely repairing an existing directory's
mode/ownership without ever touching its contents (matching
`tmpfiles.d(5)`'s own `d`-type semantics).

One narrow exception is worth stating precisely rather than glossing
over: the Foundation E provisioning lock shared by E3/E4/E5
(`isadoraair.runtime_provisioning.runtime_provision_lock()`) must place
its `.provision.lock` file inside `/opt/isadoraair-runtime` before any
provisioner -- including this one -- has necessarily run, so it may
bootstrap that one directory first via a plain, unprivileged
`mkdir`+`chmod` at the same `0755` mode this contract requires. That
bootstrap is a safe equivalent default, not a second authority: it only
ever creates the directory if absent and never adjusts an existing
directory's mode or ownership, so it can race ahead of but never
disagree with `systemd-tmpfiles`. `systemd-tmpfiles` remains the
authority that establishes and repairs both directories' ownership/mode
end to end (including a directory the lock never touched, and
repairing one the lock's bootstrap left with the wrong owner under a
non-canonical `--target-root`); the two converge on the identical
`0755`/expected-owner contract by construction, and E5's own evidence
model validates against that one contract regardless of which path
created the directory first.

`deploy/isadoraair-runtime-tmpfiles.conf` (Git-owned) is installed
verbatim to `/etc/tmpfiles.d/isadoraair-runtime.conf` (matching the
pre-existing `isadoraair-tmpfiles.conf` -> `/etc/tmpfiles.d/
isadoraair.conf` mapping convention already used by `deploy/restore/
90-system-config.sh`, which this file follows but does not itself
modify -- see "Known deferred integration gap" below). Its two `@@
ISADORAAIR_SURFACE_UID@@` / `@@ISADORAAIR_SURFACE_GID@@` markers are
substituted with plain numeric `0`/`0` for the real canonical host
(deliberately never a symbolic `root` name, avoiding any
username-resolution dependency entirely -- confirmed empirically that
`systemd-tmpfiles --root=<disposable>` cannot resolve an arbitrary
username against an empty/disposable filesystem root, but numeric IDs
always work) and with the calling identity's own numeric `uid`/`gid` for
a disposable, caller-owned `--target-root` install or test, matching
every other Foundation E provisioner's established non-canonical-root
ownership convention (E3/E4 both already require a non-canonical target
root to be caller-owned and publish everything beneath it as the
caller).

`isadoraair.runtime_surfaces._run_tmpfiles()` invokes it with the same
discipline E4 established for `ldconfig`: a fixed absolute executable
path (never `PATH`-resolved), an argv list (never a shell string), a
minimal allowlisted subprocess environment, a bounded timeout, and
bounded captured diagnostic output on failure. It always names the
installed config file explicitly (never relies on `systemd-tmpfiles`'s
own directory-scan default), so it can never process an unrelated
`*.conf` file it doesn't own. For a mapped `--target-root`, it adds
`--root=<target-root>` (confirmed, by direct invocation on this host,
to scope every directory operation to that mapped root only -- a
mapped-root test genuinely cannot create, read, or alter anything under
the real host's `/opt`, `/var/lib`, `/run`, or `/etc/tmpfiles.d`).

### Known deferred integration gap

`deploy/restore/90-system-config.sh`'s existing generic `deploy/*.conf`
install loop does not yet know the new file's correct
`/etc/tmpfiles.d/isadoraair-runtime.conf` destination (it would fall
through to that loop's systemd-unit-shaped default destination, which is
wrong for a tmpfiles config). Fixing that script is restore-implementation
work, explicitly out of E5's scope; recorded here for the later
Foundation E restore/baseline consolidation pass (E6), alongside the
already-known `check_deploy_baseline` fdkaac and `espeak-ng` prerequisite
gaps documented in `docs/RUNTIME_PROVISIONING.md` and
`docs/RUNTIME_COMPONENTS.md`.

## API: plan / apply / validate

`isadoraair.runtime_surfaces.RuntimeSystemSurfaceManager` is the reusable
API, independent of Django station configuration (unlike E3/E4's
TTS/native provisioners, establishing these surfaces requires no station
DB read at all -- a fresh installer can call it before any station
configuration exists):

```python
from isadoraair.runtime_surfaces import RuntimeSystemSurfaceManager

manager = RuntimeSystemSurfaceManager()   # canonical / by default
plan = manager.plan()                     # read-only; never mutates
result = manager.apply()                  # idempotent; requires privilege for canonical /
evidence = manager.current_evidence()     # read-only structured evidence
```

`plan()` is always read-only. `apply()` beneath the real canonical `/`
requires the calling process to actually be root; beneath a
`--target-root`, the target must already exist and be owned by the
caller -- matching E3/E4's own established privilege boundary exactly,
with no internal `sudo` anywhere.

### Shared Foundation E provisioning lock

`apply()` uses the exact same `isadoraair.runtime_provisioning.
runtime_provision_lock()` context manager E3's `RuntimeProvisioner.apply()`
and E4's `NativeRuntimeProvisioner.publish()` already use -- the same
`fcntl.flock()`-guarded `<runtime_root>/.provision.lock` file. An E5
system-surface apply, an E3 TTS generation publish, and an E4 native
fdkaac publish therefore cannot mutate canonical Foundation E state
concurrently; whichever acquires the lock second blocks until the first
releases, then re-plans against the now-current state before deciding
whether there is any work left to do (see `test_native_uses_real_common_
cross_process_provision_lock`-style coverage in
`isadoraair/tests/test_runtime_surfaces.py`, using real
`multiprocessing` processes and real `flock()` -- never a mocked lock
seam).

### Evidence and structured state

Each surface reports one of `absent` / `wrong_type` / `symlink` /
`wrong_owner` / `unsafe_permissions` / `wrong_content` / `healthy` --
never a single collapsed "missing runtime". This is deliberately the
same shape E1/E2's `ComponentEvidence`/`RuntimeEvidence` already use, so
a future E6 baseline consolidation can present E1-E5 evidence uniformly.
Validation never repairs; it only ever reports what `apply()`'s plan
would intend to do.

## Idempotence and rollback

If every surface is already healthy, `apply()` acquires the shared lock,
re-validates, and returns `no_op` -- performing no launcher replacement,
no tmpfiles-config replacement, no `systemd-tmpfiles` execution, and no
directory churn. Real disposable-root testing proves this exactly (see
`ApplyIdempotenceTests`/`test_first_apply_installs_and_second_is_exact_
no_op`).

Product-owned **files** (the launcher, the installed tmpfiles config) are
snapshot-and-restored on any failure after they've been touched, using
the same same-directory-temp + fsync + atomic-replace primitive E3/E4
already use (`isadoraair.runtime_provisioning._write_atomic`). Rollback
failure preserves the original triggering failure as `__cause__` rather
than masking it, matching E3/E4's own established pattern.

**Directory establishment is deliberately not transactional the same
way.** If E5 creates a previously-absent, correctly-owned, empty
`/opt/isadoraair-runtime` or `/var/lib/isadoraair/tts` and a *later* step
in the same `apply()` call fails, that directory is left in place rather
than deleted -- deleting it would risk destroying E3/E4 generation trees
that may already exist inside it from an earlier, unrelated apply, and a
correctly-owned empty (or already-populated) directory is never itself
an unsafe state to leave behind. E5 never recursively `chown`s or
`chmod`s an existing tree, and never deletes
`/opt/isadoraair-runtime`, `/var/lib/isadoraair/tts`, or anything beneath
them as a repair or rollback strategy -- confirmed by dedicated tests
proving pre-existing E3-shaped generation directories survive both a
successful no-op apply and a failed, rolled-back one untouched.

## Management-command interface

E5 extends the existing `provision_runtime_components` command (Option A
from the review scope) rather than adding a second command: the command
was already a generic "Foundation E provisioning adapter" spanning E3
TTS and E4 native modes via mode flags, and system surfaces is a genuine
peer third mode of the same underlying operation family (plan vs. apply
against one of the shared-lock-protected Foundation E surfaces), not a
distinct kind of operation the way, say, backup would be.

```bash
python manage.py provision_runtime_components --surfaces --plan  [--target-root PATH] [--json]
python manage.py provision_runtime_components --surfaces --apply [--target-root PATH] [--json]
```

- `--surfaces` is required to reach this mode at all; it cannot be
  combined with `--bundle` or any E3/E4-specific flag (`--fdkaac`,
  `--prepare-fdkaac`, `--publish-fdkaac`, `--native-source-dir`,
  `--prepared-native-root`, `--trusted-preparer-uid`,
  `--bootstrap-fdkaac`) -- each combination is explicitly rejected with a
  `CommandError`, not silently ignored.
- Exactly one of `--plan`/`--apply` is required (the pre-existing
  mutually-exclusive `mode` group); omission is a hard error, never an
  implied mutation.
- Canonical (`/`) `--apply` requires real root privilege; a
  `--target-root` requires the caller to already own that directory --
  enforced by `RuntimeSystemSurfaceManager` itself, not by the command.
- `--json` produces the same deterministic `to_json()` output the
  reusable API objects already produce -- future restore/installer code
  is expected to consume `RuntimeSystemSurfaceManager`/`SystemSurfacePlan`
  /`SystemSurfaceResult` directly, never to parse this command's text or
  JSON output as its own API.

## No caller migration

`/usr/local/bin/isadoraair-tts` now exists (once `apply()`d) but nothing
is repointed at it. `webrequests/services.py`, `road_conditions/
synthesis.py`, and any other historical `KOKORO_BINARY`/weather-ingest
caller are untouched by E5 and continue exactly as before. Caller
migration is deliberately a later, independently reversible activation
decision.
