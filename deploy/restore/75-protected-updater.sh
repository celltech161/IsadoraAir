#!/usr/bin/env bash
# deploy/restore/75-protected-updater.sh -- IsadoraAir 1.2 / Runtime
# Foundation E7B / Phase-D protected updater.
#
# Backup-based disaster recovery only -- unlike 50/70 there is no
# "legacy connected install" mode for this component: the protected
# updater's trust material, signed generations, and runtime state can
# only ever come from a genuine prior backup, never freshly built or
# downloaded. With no --archive, a legacy/non-self-contained archive,
# or a self-contained archive that never declared protected_updater,
# this stage is a clean no-op PASS -- there is nothing to restore, same
# as 50/70's own "component not in this archive" no-op.
#
# Two steps, both delegated to the real Foundation E/Phase-D authority
# (monitoring/management/commands/restore_phase_d_component.py, via
# isadoraair.phase_d_recovery/isadoraair.runtime_recovery) -- this stage
# does not re-implement any trust/signature/descriptor verification or
# copy logic itself:
#   1. Offline, non-privileged-by-design restore into a throwaway fake
#      root -- full Phase-D verification, proving this exact payload
#      reconstructs a genuine, internally consistent protected-updater
#      generation (isadoraair.phase_d_recovery.restore_phase_d_component).
#   2. Publish that fake root's tree onto the real/staging restore
#      target (--publish-root), so the runtime-recovery receipt this
#      stage records afterward means what it means for every other
#      component (kokoro/piper/native_fdkaac): genuinely present at the
#      restore target, not merely proven reconstructable in a throwaway
#      directory that then gets discarded. Refuses -- never silently
#      overwrites -- any file already at its destination (a stale/
#      partial prior restore attempt must be dealt with explicitly).
#
# Real (non-staging) --apply runs the whole restore+publish under sudo
# for root-owned destinations, matching deploy/restore/
# 90-system-config.sh's own established USE_SUDO idiom exactly (`sudo
# "$VENV_PY" manage.py ...`) -- never relaxes ownership/mode checks,
# never chowns after the fact; ownership simply falls out of whichever
# effective UID performs the copy.
#
# NEVER starts, enables, or reloads anything, and NEVER creates a new
# protected-runtime generation -- this stage reconstructs EXACTLY the
# generation the backup recorded, nothing more. Activating the restored
# generation (real supervisor start under root-owned protected
# ancestry, DISARMED readiness proof) remains a deliberate, separate,
# privileged step outside this stage's scope -- see
# docs/RUNTIME_BACKUP_PAYLOAD.md's "Phase-D protected updater recovery
# extension" section for why that boundary exists and stays in place
# here.
#
# Usage:
#   deploy/restore/75-protected-updater.sh --archive PATH [--plan|--apply]
#     [--staging-root PATH]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

restore_parse_common_args "$@"
set -- "${RESTORE_REMAINING_ARGS[@]}"

if [ $# -gt 0 ]; then
  log_error "75-protected-updater.sh: unrecognized argument: $1"
  exit 2
fi

log_info "=== 75-protected-updater (Phase-D protected updater) ==="
guard_production_target

if [ -z "$RESTORE_ARCHIVE" ]; then
  log_info "75-protected-updater: no --archive given -- this component has no connected-install path, nothing to restore. PASS"
  exit 0
fi

if [ -n "$RESTORE_STAGING_ROOT" ]; then
  PUBLISH_ROOT="$RESTORE_STAGING_ROOT"
  USE_SUDO=0
else
  PUBLISH_ROOT="/"
  USE_SUDO=1
fi
log_info "Protected-updater publish target: $PUBLISH_ROOT"

if [ "$RESTORE_MODE" != "apply" ]; then
  log_plan "locate + validate the runtime-recovery/ payload embedded in $RESTORE_ARCHIVE"
  log_plan "manage.py restore_phase_d_component --recovery-payload <payload> --fake-root <tmp> --publish-root $PUBLISH_ROOT"
  log_info "75-protected-updater: PLAN complete"
  exit 0
fi

VENV_PYTHON="$RESTORE_TARGET_ROOT/venv/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
  log_error "$VENV_PYTHON not found -- Phase-D recovery delegation needs the restored app's Python environment to invoke it (it runs as a manage.py command). Run 60-python.sh before 75-protected-updater.sh for a backup-based restore (it already runs before this stage in restore.sh's own order -- see deploy/restore/README.md's dependency map)."
  exit 1
fi
if [ ! -f "$RESTORE_TARGET_ROOT/manage.py" ]; then
  log_error "$RESTORE_TARGET_ROOT/manage.py not found -- run 20-application.sh first."
  exit 1
fi

WORKDIR="$(mktemp -d /tmp/isadoraair-restore-protected-updater.XXXXXX)"
cleanup_protected_updater_recovery() { rm -rf "$WORKDIR"; }
trap cleanup_protected_updater_recovery EXIT
PAYLOAD_DIR="$WORKDIR/payload"
FAKE_ROOT="$WORKDIR/fake-root"

restore_locate_recovery_payload "$PAYLOAD_DIR"
if [ "$RESTORE_RECOVERY_PAYLOAD_FOUND" -ne 1 ]; then
  log_info "75-protected-updater: legacy/non-self-contained archive -- no runtime-recovery payload embedded, nothing to restore. PASS"
  exit 0
fi

log_apply "validating runtime-recovery payload at $PAYLOAD_DIR"
RECOVERY_EVIDENCE_JSON=$("$VENV_PYTHON" "$RESTORE_TARGET_ROOT/manage.py" validate_runtime_recovery_payload "$PAYLOAD_DIR" --json)
PROTECTED_UPDATER_STATE=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["components"]["protected_updater"]["state"])' "$RECOVERY_EVIDENCE_JSON")
if [ "$PROTECTED_UPDATER_STATE" != "present" ]; then
  log_info "75-protected-updater: no protected_updater component in this archive (state=$PROTECTED_UPDATER_STATE) -- this station's recovery policy did not include it, or this is a pre-Phase-D archive. Nothing to restore. PASS"
  exit 0
fi

RESTORE_CMD=("$VENV_PYTHON" manage.py restore_phase_d_component
  --recovery-payload "$PAYLOAD_DIR" --fake-root "$FAKE_ROOT" --publish-root "$PUBLISH_ROOT")
if [ "$USE_SUDO" -eq 1 ]; then
  RESTORE_CMD=(sudo "${RESTORE_CMD[@]}")
fi
log_apply "${RESTORE_CMD[*]}"
( cd "$RESTORE_TARGET_ROOT" && "${RESTORE_CMD[@]}" )

restore_record_recovery_components protected_updater >/dev/null

log_info "75-protected-updater: PASS (protected_updater recovered from the Runtime Foundation E7 payload and published to $PUBLISH_ROOT; activation remains a separate, privileged, deliberate step -- see docs/RUNTIME_BACKUP_PAYLOAD.md)"
exit 0
