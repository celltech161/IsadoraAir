# r0123 installation and verification

Publication does not authorize installation. KOGR and WRJE each require a separate operator decision
and their own Update Center preview. Do not assume that either station is currently on r0122.

## Identities

- **R** is the unique commit introducing `deploy/releases/r0123.json`. It is the installable release.
- **C** is R's direct metadata-only child adding
  `deploy/migration_authorizations/r0123.json`. It is the trusted repository tip used to authorize
  migration operations; the station checkout installs R, not C.

## Before installation — each station

1. Record the actual installed release, exact HEAD and branch, and confirm the checkout is clean.
2. Confirm the protected updater is ready and its runtime inventory/signature validation is healthy.
3. Confirm current backup/checkpoint and recovery evidence meets station policy.
4. Confirm no long-form waveform refresh is running. Verify the application user can read source
   media, write `WAVEFORMS_DIR`, create `WAVEFORMS_DIR/.locks`, and execute FFmpeg/FFprobe.
5. Obtain the station's actual Update Center preview. From r0122 it must show release `[r0123]`,
   migration `library.0099_track_cue_marker_authority`, exactly two manual operations (indices 0 and
   1, both `AddField`) covered by the r0123 companion, no collectstatic/package/unit/nginx/runtime
   action, and restarts exactly Gunicorn then active-message worker.
6. A station on r0121 should show `[r0122, r0123]` with the same single migration and restart set.
   Any other installed baseline requires review of its actual multi-release plan; do not extrapolate.
7. Run `docs/releases/r0123_verify.sql` read-only and retain its pre-install marker, Ready-to-Air,
   relationship and schema counts. Stop if the preview or trust evidence differs.

## Installation

Use only the normal trusted Update Center workflow. It applies migration 0099 under the existing
checkpoint and migration-plan gates, using C's two exact authorizations, then restarts:

1. `isadoraair-gunicorn`
2. `isadoraair-active-message-worker`

Do not bypass a migration refusal with a manual migration, local approval, altered companion or
edited migration. Do not enable Hour Refill or change unrelated station settings.

## After installation

1. Confirm the Update Center job succeeded, the installed release is r0123, station HEAD is R, the
   checkout is clean, and the trusted remote tip contains C.
2. Confirm migration 0099 is recorded and `manage.py check` has no new issue.
3. Confirm Gunicorn and active-message worker are active and healthy. Confirm the protected updater
   runtime remains unchanged and valid; no engine or Monitoring restart should have occurred.
4. Re-run `docs/releases/r0123_verify.sql`. Compare counts with the retained pre-install output:
   existing marker values, Ready-to-Air and Track relationships must be unchanged; existing rows
   should begin with `{}` state, revision `0` and null analysis request unless used since install.
5. Confirm no bulk waveform regeneration, source-media conversion, cue-marker rewrite or orphan
   cleanup occurred.

## Controlled field acceptance

Use a controlled, non-airing Track for every state-changing check:

- inspect a historical marker and confirm it is protected;
- lock, unlock and approve one marker, then confirm revision and history behavior;
- save metadata only and confirm marker state is unchanged;
- run Refresh Waveform Only and confirm source audio and metadata are unchanged;
- Repick an unlocked marker while confirming a locked marker is preserved;
- inspect previously repaired waveform displays, including Track 1343;
- verify exact long-form refresh progress/completion on an authorized test Track; and
- inspect Monitoring card readability and existing warning visibility without manufacturing an
  outage.

Record R, C, plan identity, companion coverage, migration evidence, restart results and field checks
separately for KOGR and WRJE.
