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
# Canonical / checks, all read-only:
#   1. manage.py check -- Django's own system check framework.
#   2. manage.py check_deploy_baseline -- Phase 3's preflight command,
#      consolidated onto Runtime Foundation E evidence by Runtime
#      Foundation E6 (docs/RUNTIME_DEPLOY_BASELINE.md): covers
#      PostgreSQL tools, GStreamer + every required element
#      (docs/GSTREAMER_ELEMENT_INVENTORY.md), Liquidsoap, snd-aloop, the
#      E5 system surfaces, the TTS scratch surface, package
#      prerequisites, and fdkaac/Kokoro/Piper via Foundation E's own
#      E1/E2 evidence, in one pass. This is where Phase 4 spec section
#      25/26's GStreamer-element and Liquidsoap verification
#      requirements are actually satisfied -- not reimplemented here,
#      called.
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
# A --staging-root is an offline filesystem, not a booted host. In that
# mode this stage runs only target-mapped structural validation: E5 and
# scratch/tmpfiles/application/library filesystem evidence plus product
# contract validation. It never borrows the installer host's DB, kernel,
# runtime processes, identities, or canonical filesystem surfaces.
#
# Usage:
#   deploy/restore/95-validate.sh [--plan|--apply] [--staging-root PATH]
#     [--isa-user USER]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

restore_parse_common_args "$@"
set -- "${RESTORE_REMAINING_ARGS[@]}"

# Same ISA_USER resolution deploy/restore/90-system-config.sh's own
# --isa-user/`id -un` convention uses -- threaded through to
# `manage.py check_deploy_baseline` below so its Runtime Foundation E6
# TTS-scratch-surface evidence (/run/isadoraair/tts) can actually resolve
# the expected service identity here, rather than reporting it
# unresolved on every single post-restore run.
ISA_USER="$(id -un)"
while [ $# -gt 0 ]; do
  case "$1" in
    --isa-user) ISA_USER="${2:?}"; shift 2 ;;
    --isa-user=*) ISA_USER="${1#*=}"; shift ;;
    *) log_error "95-validate.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done

log_info "=== 95-validate ==="
guard_production_target

VENV_PY="$RESTORE_TARGET_ROOT/venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  log_error "$VENV_PY not found -- run 60-python.sh first."
  exit 1
fi
if [ -z "$RESTORE_STAGING_ROOT" ] && [ ! -f "$RESTORE_TARGET_ROOT/.env" ]; then
  log_error "$RESTORE_TARGET_ROOT/.env not found -- run 20-application.sh first."
  exit 1
fi

if [ "$RESTORE_MODE" != "apply" ]; then
  if [ -n "$RESTORE_STAGING_ROOT" ]; then
    log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py check_deploy_baseline --structural-only --target-root $RESTORE_STAGING_ROOT --isa-user $ISA_USER"
  else
    log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py check"
    log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py check_deploy_baseline --isa-user $ISA_USER"
    log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py showmigrations"
    log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py migrate --plan"
  fi
  log_info "95-validate: PLAN complete"
  exit 0
fi

cd "$RESTORE_TARGET_ROOT"
OVERALL_OK=1

if [ -n "$RESTORE_STAGING_ROOT" ]; then
  log_info "--- Offline target structural baseline ---"
  log_info "Target service identity '$ISA_USER' will be resolved from $RESTORE_STAGING_ROOT/etc/passwd, not the installer host."
  if "$VENV_PY" manage.py check_deploy_baseline \
      --structural-only \
      --target-root "$RESTORE_STAGING_ROOT" \
      --isa-user "$ISA_USER"; then
    log_info "offline target check_deploy_baseline: PASS"
    log_info "95-validate: PASS (offline target structural/filesystem contract; live DB, station, kernel, and runtime execution checks intentionally deferred until boot-root validation)"
    exit 0
  fi
  log_error "offline target check_deploy_baseline: FAILED -- the staged filesystem is incomplete or unsafe. Installer-host state cannot satisfy this check."
  exit 1
fi

log_info "--- manage.py check ---"
if "$VENV_PY" manage.py check; then
  log_info "manage.py check: PASS"
else
  log_error "manage.py check: FAILED"
  OVERALL_OK=0
fi

log_info "--- manage.py check_deploy_baseline ---"
# Its own exit code is authoritative: 0 iff everything resolves to PASS
# (Runtime Foundation E6) -- see the command's own docstring for the
# full PASS/FAIL/UNRESOLVED contract. --isa-user threads through the
# SAME ISA_USER this stage resolved above, so the TTS scratch-surface
# evidence can actually resolve rather than reporting unresolved.
if "$VENV_PY" manage.py check_deploy_baseline --isa-user "$ISA_USER"; then
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
