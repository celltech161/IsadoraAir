# r0116 readiness assessment (integrated D3E-A/B/C/D)

**Status: development qualification evidence, not a release.** Nothing in
this document has been built, signed, published, installed or migrated.
`deploy/releases/r0116.json` and `deploy/migration_authorizations/r0116.json`
do not exist, and no value below that only the packaging stage can know (a
release commit, a manifest hash, a signature) has been invented. Packaging,
signing and deployment are separately authorized steps.

## Accepted milestone ancestry

| Milestone | Commit | Status |
| --- | --- | --- |
| D3E-A — worker lifecycle, heartbeat / readiness contract | `3a76ecc57278437fe1aa13bb5499431a761d2a2d` | accepted, frozen |
| D3E-B — managed worker unit, Monitoring, runtime-13 restart authority, generation-15 policy source | `17f080acd09b5bd66a522730c081534f5f7ef317` | accepted, frozen |
| D3E-C — retention, reconciliation, quarantine, Track write guard | `ff8c20573cf2c29bee3af155400d1830a1d89496` | accepted, frozen |
| D3E-D — board diagnostics, long-session refresh, this assessment | branch `feature/2.22d3e-d-operator-diagnostics` (record its final SHA in the release-introducing commit message) | candidate, under review |

Each is an ancestor of the next (`git merge-base --is-ancestor`). D3E-D
changes only `active_messages/` (read-only board views, templates, the
reconciler's shared ownership scan and inspection helpers), tests and
documentation; it adds no migration, model, setting, capability, unit, timer
or protected-runtime change, so every D3E-B packaging prerequisite below is
exactly as D3E-B left it.

## What r0116 must deliver

1. The application source at the D3E-D candidate (D3E-A..D).
2. Protected generation **15**: runtime **13** plus the signed policy entry
   `isadoraair-active-message-worker.service: ENABLE_NOW`.
3. The managed unit `isadoraair-active-message-worker.service`.
4. The additive Monitoring migration `monitoring.0014`.

## Packaging prerequisites (verified in source at the D3E-D candidate)

| # | Prerequisite | Where / how verified |
| --- | --- | --- |
| 1 | Protected updater runtime version **13** | `deploy/updater_runtime/isadoraair_updater/__init__.py`: `RUNTIME_VERSION = 13`; `updatecenter/tests/test_active_message_worker_restart.py` |
| 2 | Runtime-13 restart authority: `isadoraair-active-message-worker` in the protected `CORE_SERVICES`, the Django manifest's `CORE_RESTARTABLE_SERVICES`, and both identical `RESTART_ORDER` tables (gunicorn, engine, monitoring, spoken-render-worker, active-message-worker, encoders, rbds) | `deploy/updater_runtime/isadoraair_updater/release.py`, `updatecenter/manifest.py`, `updatecenter/planner.py`; same test |
| 3 | Generation-15 policy source names the worker `ENABLE_NOW` (unsigned source; generation 14 refuses the unit) | `deploy/updater_runtime/protected-policy.json`; `updatecenter/tests/test_active_message_worker_policy.py` |
| 4 | The unit template | `deploy/isadoraair-active-message-worker.service` (start gate, bounded restart / stop; the shared `/run/isadoraair` directory) |
| 5 | Additive Monitoring migration `monitoring.0014_provision_active_message_worker_check`, source SHA-256 **`e87ae5de003d01c857884821106b0d7a618d15e926ec84820fcf072e4a245566`** (the collision-safe version; the superseded `5d10a044…` bytes must never be authorized) | `sha256sum monitoring/migrations/0014_provision_active_message_worker_check.py` at the candidate; `monitoring/tests/test_active_message_worker_migration.py` |
| 6 | No operator-facing Restart for the worker (Monitoring withholds it; the board has none) | `monitoring/services/probes.py` (`restart_via_dashboard = False`); `monitoring/tests/test_active_message_worker_check.py`; `active_messages/tests/test_board_diagnostics.py` |
| 7 | Native airing closed | `active_messages/services.py`: `NATIVE_AIRING_AVAILABLE = False` (a source constant, not configuration) |

## The separate, authorized packaging stage — steps and inputs

Inputs: the reviewed D3E-D candidate commit; the reviewed protected runtime
tree; the offline release signer and the trusted public key; the release
manifest content. The steps are those of docs/ACTIVE_MESSAGES.md
("Protected deployment (r0116)"), in order:

1. Build the generation-15 descriptor from the reviewed runtime tree
   (`deploy/updater_bootstrap/tools/protected_runtime_release.py`,
   `build_descriptor`; runtime 13, manifest protocol 5, wire 3/4). Its only
   changes from generation 14: the policy entry and the runtime-13 restart
   authority.
2. Sign the primary-release statement offline; verify it against the trusted
   public key; commit manifest, descriptor and attestation atomically in the
   release-introducing commit **R** (the r0109 pattern); validate with
   `updatecenter.protected_release_validator`.
3. Manifest: the unit in **`systemd_units_new_required`** (not `_optional`);
   `services_requiring_restart` = `isadoraair-monitoring` and
   `isadoraair-gunicorn` — **never the worker** (bridge rule: a runtime-12
   worker refuses the new name; `enable --now` starts it once on r0116 code;
   only releases after r0116 may declare its restart).
4. `migrations_required`: `monitoring.0014_provision_active_message_worker_check`,
   `migration_compatibility: "additive"`. Its one `RunPython` is classified
   **manual**, so a trusted companion is required — authored only after R
   exists:
   1. record R and `M = sha256(deploy/releases/r0116.json at R)`;
   2. confirm the migration's SHA-256 at R is `e87ae5de…a245566` and that
      `updatecenter_probe` reports exactly one manual operation (index 0,
      `RunPython`);
   3. write `deploy/migration_authorizations/r0116.json` (schema 1,
      `release_id` r0116, `target_commit` R, `manifest_sha256` M, exactly one
      `authorized_manual_operations` entry: that ref, that file SHA-256,
      `operation_index` 0, `RunPython`, `manual`; `json.dumps(..., indent=2)`
      plus a newline);
   4. commit it alone as a single-parent child of R changing only that file;
      never amend R or the companion;
   5. validate with both validators at the new tip; publish both commits
      together.
5. After installation, verify: the "Active Message Worker" Monitoring card
   present and `ok`; on the Operator Board, the worker chip "Worker OK" and
   the diagnostics page showing process identity **verified** and heartbeat
   **current**; the gate still closed; a fresh Runtime Recovery payload for
   generation 15.

## Recovery and rollback considerations

There is no automatic rollback (docs/UPDATE_CENTER.md). Generation 14 stays
the last-known-good protected slot; fix forward with a corrected signed
release. Never hand-install or edit units, the policy, trust configuration or
migrations. Nothing below can open native airing or touch OGRemote, the
engine or station playback.

| Condition | What the station shows | Response |
| --- | --- | --- |
| Worker not installed (before r0116, or the job failed before install) | board: "Worker check not provisioned" (no `monitoring.0014`) or Monitoring: no unit | expected before r0116; after a failed job, fix forward with a corrected release |
| Worker installed but not running | Monitoring critical (`service_inactive` / `service_failed`); board chip "Worker critical" | the unit retries with back-off; with the gate closed this is harmless; read the journal; fix forward |
| Heartbeat missing / stale / from another process | Monitoring `no_heartbeat` / `stale` / `heartbeat_not_current`; board identity "unknown" / "not current" | never inferred healthy; investigate the worker process; no manual restart path on the board |
| Monitoring check not provisioned | board "Worker check not provisioned" | `monitoring.0014` not applied — the release's authorized migration step |
| Additive migration not applied | as above; the release job reports the manual operation awaiting its companion | apply only through the release's trusted companion; never by hand |
| Protected runtime not yet advanced (still runtime 12 / generation 14) | the updater refuses the new unit name (`UNKNOWN_MANAGED_UNIT`) | expected until generation 15 verifies; never declare the worker restart in r0116 |
| Publication retention failure | board diagnostics: `retired_bytes_remaining` (in progress) or integrity / unknown findings | `manage.py active_message_retention --sweep --apply` after fixing the cause (storage, permissions) |
| Quarantine pending / manual recovery | board diagnostics: "Quarantine pending" / "Manual recovery required" | sweep re-proves and finishes pending items; manual items per docs/ACTIVE_MESSAGES.md (never delete a payload by hand without comparison) |
| Native airing gate closed | board "Native airing closed"; Replay Now unavailable | expected until Phase G |
| Partial installation (job failed midway) | the unit installed and enabled, retrying; schema and checkout at the release | harmless with the gate closed; fix forward; DR procedures keep station playback and OGRemote authority unchanged |

## Qualification evidence

Measured at the D3E-D code commit `7308974a43c8625cfe20f142486fd2148c75d6a3`
(the commits after it change documentation only), serially, `umask 0022`,
through `production/tests/run_with_validation_service.sh` with the dev
interpreter, an isolated test database and disposable roots. Nothing ran
against a live protected runtime, TTS provider, station audio, updater key,
systemd action or production database.

| Suite | Result at the D3E-D candidate |
| --- | --- |
| `active_messages` (incl. 41 diagnostics and 9 long-session browser tests) | 696 OK |
| `production.tests.test_retention`, `test_reconcile` | 50 OK |
| `produced_content` (iPortal produced content) | 123 OK |
| `library` | 1812 OK |
| `spoken`, `weather`, `aircheck` | 719 OK |
| `monitoring` | 561, 1 failure (baseline, below) |
| `updatecenter` | 1393 OK |
| protected-runtime focused (`test_recovery_freshness`, `test_weather_ingest_monorepo`, `test_phase_d_recovery`, `test_db_credentials`) | 133 OK (1 skipped) |
| complete `isadoraair` | 1622: 10 failures, 2 errors, 7 skipped (all baseline, below) |
| render / encoder qualification (`production.tests.test_render_service`, `encoders.tests.test_candidate_qualification`, `encoders.tests.test_reconciliation`) | 168 OK |
| `production.tests.test_render`, `test_render_readiness_gate` | 27 OK |
| complete `encoders` | 487 OK |

Also: Django system check clean; `makemigrations --check` "No changes
detected"; `compileall` clean; `git diff --check` clean; `monitoring.0014`
SHA-256 `e87ae5de…a245566` unchanged.

Baseline results (none attributable to D3E-D, which changes nothing under
`isadoraair/`, `deploy/`, `monitoring/` or `encoders/`):

* Monitoring `test_probe_encoder_group…test_old_healthy_pair_cannot_survive_a_real_restart`
  — the established fdkaac fixture failure, identical on accepted baselines.
* Complete `isadoraair`, 11 results classified against accepted D3E-B on
  2026-10-20 (identical assertions there): 6 need `<repo>/venv/bin/python`
  (2 restore-ledger errors; `test_tts_cli` 3 methods / 4 results — 4 of the 6
  pass on both revisions with an equivalent disposable environment; the two
  external-provider subtests were deliberately not run against the host's
  protected TTS runtime and are NOT reported as passing); 2 restore-script
  comment matches; 2 host-state assumptions; 1 Kokoro voice-catalog match.
* Complete `isadoraair`, 1 intermittent:
  `test_recovery_media_routing.RestoreShFunctionSourcingTests.test_explicit_media_root_is_deterministic_non_tty`
  compares two runs' full output, which carries `[…THH:MM:SSZ]` log
  timestamps, so it fails whenever the two runs straddle a second
  (observed `…:49Z` vs `…:50Z`). Isolated: 1 of 5 at D3E-D; 2 of 28 at
  accepted D3E-B — a wall-clock flake of the baseline.

## Test-maintenance recommendations (separate work; not changed here)

The complete `isadoraair` suite in a development worktree has 11 results that
are identical on accepted D3E-B and are not regressions (classified against
`17f080a`, 2026-10-20). Proposed hermetic corrections — none changes
restore, updater, backup, TTS or systemd production behavior:

1. `test_restore_resume_ledger.WeatherDataDirDjangoImportRegressionTests` (2)
   and `test_tts_cli` (3 methods, 4 results): they execute
   `<repo>/venv/bin/python`, which only a production checkout has. Use a
   test-provided interpreter (e.g. `sys.executable` through a parameter of
   the launcher under test) or skip with an explicit reason when the
   repository-local environment is absent. The `cli-kokoro` subtest must keep
   avoiding the host's protected TTS runtime: give it a fake runtime manifest.
2. `test_runtime_surfaces` (2): they assert that the real host paths
   `/usr/local/bin/isadoraair-tts` and `/opt/isadoraair-runtime` do not
   exist, which is false on a station. Assert instead that a mapped apply
   leaves those paths' state (absent or present) unchanged.
3. `test_backup_verify_units` and `RestoreResumeStaticSafetyTests` (2): they
   match the text `systemctl enable` inside a comment of
   `deploy/restore/90-system-config.sh` that states the stage never does it.
   Match only executable lines (strip shell comments) — the script makes no
   `systemctl` call.
4. `test_tts_service.DependencyAndHostIsolationTests` (1): the station-voice
   check matches `af_jessica` in `KOKORO_PROVIDER_VOICE_IDS`, Kokoro's full
   provider catalog. Exclude the provider catalog module (or the catalog
   constant) from the station-assumption scan.
5. `test_recovery_media_routing…test_explicit_media_root_is_deterministic_non_tty`:
   compare the two runs' output with the log timestamps stripped (or with a
   fixed clock), so a second boundary between them cannot fail it.
6. Monitoring `test_old_healthy_pair_cannot_survive_a_real_restart`: the
   established fdkaac test-fixture failure, reproduced on accepted baselines;
   correct the fixture, never the production fdkaac / Liquidsoap validation.
