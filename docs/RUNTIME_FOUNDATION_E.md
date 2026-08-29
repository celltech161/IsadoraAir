# Runtime Foundation E — phase index

"Runtime Foundation E" is the umbrella name for the sequence of phases
that took IsadoraAir's TTS/native runtime from a set of pre-existing,
disconnected, host-specific scripts to one coherent, machine-readable,
canonically-pathed, restore-integrated system. Each phase has its own
detailed doc; this file is only the map between them.

| Phase | Scope | Key doc(s) |
|---|---|---|
| **E1/E2** | Machine-readable runtime-component contract (`runtime_components.json`) + pure requirement resolution (`runtime_requirements.py`) + read-only evidence/validation (`runtime_validation.py`) — the authority for "what does this station need, and does it currently pass". | `docs/RUNTIME_COMPONENTS.md`, `docs/RUNTIME_VALIDATION.md` |
| **E3** | Offline TTS (Kokoro/Piper) runtime provisioning/publication from a pre-staged bundle — generation-based, atomic, rollback-capable. | `docs/RUNTIME_PROVISIONING.md` |
| **E4** | Native fdkaac prepare/publish (unprivileged build validation, then protected canonical publication) — shares E3's provisioning lock and atomic-publication primitives. | `docs/RUNTIME_PROVISIONING.md` |
| **E5** | Canonical, stable OS-level filesystem/CLI surfaces: the installed `isadoraair-tts` launcher, `/opt/isadoraair-runtime`, `/var/lib/isadoraair/tts`, and their `systemd-tmpfiles` config — independent of `--target-root`'s file-placement mapping. | `docs/RUNTIME_SYSTEM_SURFACES.md` |
| **E6** | Baseline + restore consolidation: DB-independent structural and canonical live/station tiers; target-root-aware offline restore validation; legacy preflight and `deploy/restore/` aggregated onto E1–E5 evidence; closes the fdkaac pkg-config false-negative; fail-closed component→Ubuntu-package-group relationships; target-identity/safe-ancestry scratch evidence; E5's tmpfiles config at its correct restore destination. | `docs/RUNTIME_DEPLOY_BASELINE.md` |
| **E7** *(deferred)* | Backup v3 payload construction and installer/restore consumption of it — actually shipping the wheel/model/native-source bundles E3/E4 provision from, and wiring `deploy/restore/50-native-deps.sh`/`70-tts.sh` onto E3/E4's own canonical provisioners instead of their current pre-Foundation-E mechanisms. | *(not yet written)* |
| **E8** *(deferred)* | Fully offline, disposable, whole-machine acceptance — an end-to-end restore/provision/validate run with no network access at all, once E7's payload consumption exists. | *(not yet written)* |

## Reading order

For a first read of the whole sequence: E1/E2 → E3 → E4 → E5 → E6, in
that order — each phase's doc assumes the previous ones' vocabulary
(canonical paths, the plan/apply/validate pattern, the shared
provisioning lock, structured evidence) without re-explaining it.

## Cross-cutting conventions established across every phase

- **Canonical paths come from one place**: `runtime_components.json`'s
  `canonical_paths` block. No phase hardcodes `/opt/isadoraair` (or any
  other canonical path) a second time in Python.
- **`--target-root` maps file *placement*, never embedded *content*.**
  Established by E5 for the installed launcher; E6's restore integration
  relies on this holding, unmodified, for a staging-root restore.
- **Structured evidence, not booleans.** Every phase's validator returns
  a small, JSON-serializable evidence object with an explicit state
  vocabulary (E1/E2: `pass`/`fail`/`optional_absent`; E5: `absent`/
  `wrong_type`/`symlink`/`wrong_owner`/`unsafe_permissions`/
  `wrong_content`/`healthy`; E6 layers `unresolved`/`not_applicable` on
  top for a fail-closed answer to "we genuinely don't know yet").
- **Read-only validation, explicit apply.** Every `plan()`/`current_evidence()`/
  `validate_*()` function across every phase never mutates anything;
  provisioning/establishment is always a separate, explicitly-invoked
  `apply()`.
- **No network fallback inside validation or baseline.** Fetching
  anything over the network is always a deliberate, separate,
  explicitly-invoked step (E3/E4's bundle acquisition, `10-packages.sh`'s
  `apt-get`) — never something a read-only check does on your behalf.
