# Disaster Recovery Physical Acceptance — P0 1.2

This document records the first real clean-machine/bare-metal acceptance drill
for IsadoraAir disaster recovery and the corrective implementation introduced
immediately afterward.

## Physical drill result

The r0084 recovery authority successfully restored a fresh Ubuntu 26.04.1 host
from offline recovery media through all twelve restore stages. The database,
application checkout, Python environments, Runtime Foundation components,
companion repositories, nginx/Django health, and the persistent `/srv/isadoraair`
data disk were all recoverable. The drill nevertheless exposed three defects
that staging-only acceptance had not been able to reveal.

### 1. Restore privilege context

The USB launcher invoked the whole restore under `sudo`. The numbered restore
stages are intentionally designed to run as the ordinary IsadoraAir operator
and invoke `sudo` internally only for the privileged writes they own. Running
the orchestrator itself as root therefore changed `$HOME`, the default owner,
and the identity used by later validation.

Observed consequences included:

- `/opt/isadoraair/.env` and venv content owned by root instead of `jreed`;
- companion repositories/venvs provisioned below `/root`;
- Stage 90 rendering `/root/...` companion paths into systemd units;
- Stage 95 passing checks as root that the real service account could not pass.

`deploy/restore/bare_metal_restore.sh` is now the canonical real-machine
entrypoint. It refuses uid 0, requires the caller to equal `--isa-user`, runs
the existing restore orchestrator unchanged under that ordinary identity, and
allows the mature stages to use their existing internal `sudo` boundaries.

Do **not** run the canonical bare-metal wrapper with `sudo`.

## 2. Stale protected-updater recovery payload

Production application state was r0084 with protected runtime generation 5,
but `/var/lib/isadoraair/runtime-recovery/current` still selected a valid
September 3 generation-2 payload. Nightly backup validation established that a
`protected_updater` component was internally valid and present, but did not
establish that it was current for the application checkout being backed up.
Consequently an archive could be labelled `self_contained_v3` while embedding
an obsolete updater runtime.

The immediate production payload was refreshed and activated as
`phase-d-r0084-generation5`; a new remote backup was then proven byte-identical
to its receipt and shown to contain generation 5.

The permanent invariant is now enforced by the existing automatic recovery
policy path used by `deploy/backup_isadoraair.sh`:

- validate the embedded protected updater normally (signature/inventory/trust);
- compare its active generation and descriptor SHA with
  `deploy/updater_runtime/protected-runtime-descriptor.json` from the exact
  application checkout running the backup;
- fail closed if either generation or descriptor differs, or if freshness
  cannot be proven.

This comparison is intentionally attached to
`validate_runtime_recovery_payload --require-current-station-policy`. Explicit
or historical/offline validation remains portable and does not require a
payload to match whichever checkout happens to inspect it later.

## 3. Replacement-host LAN identity

The backed-up `.env` contained production's private LAN address
`192.168.1.125`; the replacement sandbox received `192.168.1.111`. Restoring
`.env` byte-for-byte therefore made Django reject requests to the replacement
host until `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` were manually corrected.

`deploy/restore/rebind_host_network.py` now performs a single post-restore
machine-local rebind:

- detects the current hostname;
- detects the primary IPv4 selected by the kernel's default route, with a
  global-interface fallback;
- removes obsolete private/loopback-external IP literals inherited from the
  old host;
- preserves public DNS names and the canonical station hostname;
- adds the replacement hostname and current primary IP;
- preserves existing scheme/port patterns for CSRF origins;
- atomically rewrites only `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` while
  preserving unrelated `.env` values and mode 0600.

`bare_metal_restore.sh` performs this rebind after the normal restore and then
reruns Stage 95, so the final validation PASS refers to the configuration that
will actually be used for service bring-up.

## Acceptance evidence for the corrective pass

Focused tests prove:

- stale generation 2 is rejected against a generation-5 product descriptor;
- matching generation 5/descriptor is accepted;
- descriptor mismatch is rejected even when generation matches;
- malformed/missing freshness evidence fails closed;
- the physical `.125 -> .111` host replacement rewrites only the intended
  network settings and preserves public DNS names/secrets/mode;
- the canonical bare-metal wrapper refuses root and revalidates after rebinding;
- the scheduled backup remains on its established systemd entrypoint and uses
  the strengthened automatic validator rather than a new managed unit.

A production read-only proof against the refreshed payload returned generation
5 and the expected descriptor. The preserved September 3 generation-2 payload
returned `current=false` with the exact expected/observed mismatch.

## Final P0 acceptance still required

Before P0 1.2 is closed:

1. publish/deploy the corrective release;
2. produce a fresh backup under the strengthened validator;
3. build a **new** sealed recovery authority/media set — do not mutate the
   frozen r0084 authority in place;
4. ensure the recovery-media launcher invokes `bare_metal_restore.sh` as the
   ordinary operator, never `sudo restore.sh`;
5. perform a second clean bare-metal restore;
6. verify application/companion ownership, no `/root` path leakage, automatic
   replacement-host network rebinding, protected-runtime identity, database/
   web health, persistent storage, and controlled service bring-up.

The original r0084 authority and its first physical drill remain preserved as
failure evidence.
