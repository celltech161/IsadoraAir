#!/usr/bin/env bash
# deploy/restore/restore.sh -- IsadoraAir 1.2 Phase 4.
#
# Orchestrator: runs every numbered stage (00 through 95) in order,
# passing the same flags through to each. Equivalent to running each
# deploy/restore/NN-*.sh script by hand in sequence -- this exists for
# convenience and to guarantee the order is never accidentally scrambled,
# not because the stages need shared state beyond what's already on disk
# at $RESTORE_TARGET_ROOT.
#
# Stops at the first stage that fails (no stage swallows another's
# failure) -- fix the reported problem and re-run; every stage is
# written to be safe to re-run (see each script's own idempotence note).
#
# Usage:
#   deploy/restore/restore.sh --archive PATH [--plan|--apply]
#     [--staging-root PATH] [--force-production-target] [--force-db] [--force-env]
#     [-- <stage-specific args, passed to every stage that accepts them>]
#
# For a real Phase 5 bare-machine drill, prefer running stages
# individually and reviewing each one's output before proceeding to the
# next, rather than this orchestrator's fire-and-forget sequence --
# --plan end-to-end first is strongly recommended regardless.
#
# NOTE on chained --plan runs: --plan never writes anything, so a stage
# that depends on an EARLIER stage's real output (e.g. 30-postgresql.sh
# reading DB credentials from the .env that 20-application.sh would have
# restored) will correctly report that dependency as missing and stop --
# this is expected, not a bug in the orchestrator. A full, meaningful
# preview of the whole chain requires --staging-root --apply (isolated,
# safe to run for real) rather than --plan alone.
#
# Runtime Foundation E7B (2026-08-29): 60-python now runs BEFORE
# 50-native-deps, reversing their numeric order. This is deliberate,
# not a typo -- backup-based disaster recovery's native fdkaac now
# delegates to Foundation E4's real prepare/publish authority via
# `manage.py provision_runtime_components` (see 50-native-deps.sh's own
# header), which needs the restored app's Python environment
# (60-python.sh's job) to even run. 60-python.sh is safe to run early:
# it only needs 20-application.sh (manage.py + requirements.txt) and is
# idempotent (verifies rather than recreates if the venv already
# exists), so running it here costs nothing when it's reached again at
# its usual numeric spot. See deploy/restore/README.md's "Restore-order
# dependency map" for the full picture -- the file/stage NUMBERS stay
# as stable identifiers (`ls` sort order, individual invocation), they
# no longer imply a strict execution order on their own.
#
# r0030: 75-protected-updater.sh added, placed after 70-tts.sh (same
# app-source/venv prerequisite, no DB/nginx/companion dependency) and
# before 80-companions.sh -- restores the Phase-D protected updater
# component from an embedded runtime-recovery payload, the same
# locate/validate/publish/record-receipt shape 50/70 already use for
# their own components. See deploy/restore/README.md's dependency map.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

STAGES=(
  00-preflight.sh
  10-packages.sh
  20-application.sh
  30-postgresql.sh
  40-station-content.sh
  60-python.sh
  50-native-deps.sh
  70-tts.sh
  75-protected-updater.sh
  80-companions.sh
  90-system-config.sh
  95-validate.sh
)

echo "=== IsadoraAir restore orchestrator: ${#STAGES[@]} stages ==="
for stage in "${STAGES[@]}"; do
  echo
  echo ">>> Running $stage"
  "$SCRIPT_DIR/$stage" "$@"
done
echo
echo "=== All stages completed. ==="
echo "Nothing was started/enabled/reloaded -- see deploy/restore/README.md's"
echo "'Restore-order dependency map' and docs/DISASTER_RECOVERY_RESTORE.md's"
echo "'Service bring-up order' section for what comes next (Phase 5)."
