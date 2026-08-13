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
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

STAGES=(
  00-preflight.sh
  10-packages.sh
  20-application.sh
  30-postgresql.sh
  40-station-content.sh
  50-native-deps.sh
  60-python.sh
  70-tts.sh
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
