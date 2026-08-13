#!/usr/bin/env bash
# deploy/restore/95-validate.sh -- IsadoraAir 1.2 Phase 4.
#
# Final, entirely READ-ONLY post-restore validation pass -- runs after
# every other stage. Never migrates, never starts a service, never
# writes anything. If this stage passes, a competent administrator has
# strong, checked evidence the restore is software-complete (not
# necessarily hardware/audio-ready -- see docs/DISASTER_RECOVERY_RESTORE.md's
# software-vs-hardware-readiness distinction).
#
# What it checks, all read-only:
#   1. manage.py check -- Django's own system check framework.
#   2. manage.py check_deploy_baseline -- Phase 3's preflight command:
#      covers PostgreSQL tools, GStreamer + every required element
#      (docs/GSTREAMER_ELEMENT_INVENTORY.md), Liquidsoap, HE-AAC,
#      Kokoro/Piper, snd-aloop, and more in one pass. This is where
#      Phase 4 spec section 25/26's GStreamer-element and Liquidsoap
#      verification requirements are actually satisfied -- not
#      reimplemented here, called.
#   3. manage.py showmigrations + migrate --plan -- reports migration
#      state without applying anything. A restore against the exact
#      recorded Git SHA (see 20-application.sh) should show zero planned
#      migrations; a mismatch is reported clearly, never silently
#      auto-fixed.
#   4. Static/media readiness note -- what collectstatic needs, without
#      running it destructively (collectstatic itself is safe/additive,
#      but left as an explicit operator step for Phase 5, not implied
#      here).
#
# Usage:
#   deploy/restore/95-validate.sh [--plan|--apply] [--staging-root PATH]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

restore_parse_common_args "$@"

log_info "=== 95-validate ==="
guard_production_target

VENV_PY="$RESTORE_TARGET_ROOT/venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  log_error "$VENV_PY not found -- run 60-python.sh first."
  exit 1
fi
if [ ! -f "$RESTORE_TARGET_ROOT/.env" ]; then
  log_error "$RESTORE_TARGET_ROOT/.env not found -- run 20-application.sh first."
  exit 1
fi

if [ "$RESTORE_MODE" != "apply" ]; then
  log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py check"
  log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py check_deploy_baseline"
  log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py showmigrations"
  log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py migrate --plan"
  log_info "95-validate: PLAN complete"
  exit 0
fi

cd "$RESTORE_TARGET_ROOT"
OVERALL_OK=1

log_info "--- manage.py check ---"
if "$VENV_PY" manage.py check; then
  log_info "manage.py check: PASS"
else
  log_error "manage.py check: FAILED"
  OVERALL_OK=0
fi

log_info "--- manage.py check_deploy_baseline ---"
# Its own exit code is authoritative: 0 unless something is genuinely
# MISSING (DEGRADED/OPTIONAL states don't fail it) -- see the command's
# own docstring for the full PASS/DEGRADED/MISSING/OPTIONAL contract.
if "$VENV_PY" manage.py check_deploy_baseline; then
  log_info "check_deploy_baseline: PASS"
else
  log_error "check_deploy_baseline: reported MISSING component(s) -- see output above."
  OVERALL_OK=0
fi

log_info "--- Migration state ---"
"$VENV_PY" manage.py showmigrations
PLAN_OUTPUT=$("$VENV_PY" manage.py migrate --plan 2>&1)
echo "$PLAN_OUTPUT"
if grep -qE '^\s*\[ \]' <<< "$PLAN_OUTPUT"; then
  log_warn "migrate --plan shows unapplied migrations. A restore against the exact Git SHA recorded in the backup's MANIFEST.txt (see 20-application.sh) should normally show none -- investigate before running migrate, don't apply automatically. This restore tooling does NOT run migrate for you."
else
  log_info "migrate --plan: no pending migrations -- consistent with a restore against the exact recorded Git SHA."
fi

log_info "--- Static/media readiness (informational only -- not run) ---"
log_info "collectstatic has not been run by this stage -- it's safe/additive but left as an explicit Phase 5 operator step, same as service bring-up. STATIC_ROOT/MEDIA_ROOT ownership should match \$ISA_USER before gunicorn starts."

if [ "$OVERALL_OK" -eq 1 ]; then
  log_info "95-validate: PASS"
else
  log_error "95-validate: one or more checks failed -- see above. Do not proceed to service bring-up (Phase 5) until resolved."
  exit 1
fi
