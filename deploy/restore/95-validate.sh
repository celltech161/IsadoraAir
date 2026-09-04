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
# Target service identity: --isa-user ALONE cannot resolve an identity
# for an isolated --staging-root target -- there is no target /etc/passwd
# to look it up in (a noncanonical target resolves it only from ITS OWN
# /etc/passwd; the installer host's own identity for that same name must
# never be borrowed, see isadoraair/runtime_scratch.py's module
# docstring). Either the target has its own /etc/passwd entry for
# --isa-user, or a caller supplies a trusted explicit --isa-uid/--isa-gid
# numeric pair (the same one deploy/restore/90-system-config.sh was given
# to establish the legacy scratch surface, see that script's own header)
# -- otherwise check_deploy_baseline correctly reports the TTS scratch
# surface as UNRESOLVED_IDENTITY rather than guessing. Real (non-staging)
# validation is unaffected by any of this unless --isa-uid/--isa-gid is
# deliberately supplied there too.
#
# This stage always runs check_deploy_baseline via the RESTORED target's
# own manage.py/venv ($RESTORE_TARGET_ROOT) -- deliberately NOT routed
# through lib.sh's restore_manage (unlike stages 50/70/75/90's runtime-
# repair authorities): Stage 95 exists to prove the exact application
# that was restored (its own migrations/checks/runtime contract, as
# actually installed) is internally self-consistent, which is only
# meaningful using that tree's own code -- see deploy/restore/README.md.
#
# Usage:
#   deploy/restore/95-validate.sh [--plan|--apply] [--staging-root PATH]
#     [--isa-user USER] [--isa-uid UID --isa-gid GID]
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
# unresolved on every single post-restore run. Under --staging-root,
# --isa-user alone is not enough (see this file's own header) -- pass
# --isa-uid/--isa-gid too, the same trusted numeric pair
# 90-system-config.sh was given to establish that scratch surface.
ISA_USER="$(id -un)"
ISA_UID=""
ISA_GID=""
ACCEPT_LEGACY_RUNTIME_RECOVERY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --isa-user) ISA_USER="${2:?}"; shift 2 ;;
    --isa-user=*) ISA_USER="${1#*=}"; shift ;;
    --isa-uid) ISA_UID="${2:?}"; shift 2 ;;
    --isa-uid=*) ISA_UID="${1#*=}"; shift ;;
    --isa-gid) ISA_GID="${2:?}"; shift 2 ;;
    --isa-gid=*) ISA_GID="${1#*=}"; shift ;;
    --accept-legacy-runtime-recovery) ACCEPT_LEGACY_RUNTIME_RECOVERY=1; shift ;;
    *) log_error "95-validate.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done

# --isa-uid/--isa-gid: all-or-nothing, same rule check_deploy_baseline's
# own --isa-uid/--isa-gid enforces -- caught here too so the diagnostic
# is immediate rather than deferred to (and only visible from) the
# Django command's own CommandError.
if { [ -n "$ISA_UID" ] && [ -z "$ISA_GID" ]; } || { [ -z "$ISA_UID" ] && [ -n "$ISA_GID" ]; }; then
  log_error "95-validate.sh: --isa-uid and --isa-gid must be supplied together"
  exit 2
fi
if [ -n "$ISA_UID" ]; then
  case "$ISA_UID" in ''|*[!0-9]*) log_error "95-validate.sh: --isa-uid must be a non-negative integer"; exit 2 ;; esac
  case "$ISA_GID" in ''|*[!0-9]*) log_error "95-validate.sh: --isa-gid must be a non-negative integer"; exit 2 ;; esac
fi
CHECK_BASELINE_IDENTITY_ARGS=(--isa-user "$ISA_USER")
if [ -n "$ISA_UID" ]; then
  CHECK_BASELINE_IDENTITY_ARGS+=(--isa-uid "$ISA_UID" --isa-gid "$ISA_GID")
fi

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
  if [ -n "$RESTORE_ARCHIVE" ]; then
    log_plan "verify self-contained Runtime Foundation E archive metadata and completed-component receipt"
  fi
  if [ -n "$RESTORE_STAGING_ROOT" ]; then
    log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py check_deploy_baseline --structural-only --target-root $RESTORE_STAGING_ROOT ${CHECK_BASELINE_IDENTITY_ARGS[*]}"
  else
    log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py check"
    log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py check_deploy_baseline ${CHECK_BASELINE_IDENTITY_ARGS[*]}"
    log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py showmigrations"
    log_plan "cd $RESTORE_TARGET_ROOT && venv/bin/python manage.py migrate --plan"
  fi
  log_info "95-validate: PLAN complete"
  exit 0
fi

cd "$RESTORE_TARGET_ROOT"
OVERALL_OK=1

if [ -n "$RESTORE_ARCHIVE" ]; then
  log_info "--- Runtime Foundation E archive recovery acceptance ---"
  if [ "$ACCEPT_LEGACY_RUNTIME_RECOVERY" -eq 1 ]; then
    log_warn "LEGACY ARCHIVE -- NOT SELF-CONTAINED FOR FOUNDATION E. Operator explicitly accepted connected/manual runtime reconstruction; this is not backup-v3 evidence."
  elif restore_accept_recovery_receipt; then
    log_info "Runtime Foundation E archive recovery receipt: PASS"
  else
    log_error "Runtime Foundation E archive recovery acceptance: FAILED. Required components were not positively reconstructed from this exact self-contained archive."
    exit 1
  fi
fi

if [ -n "$RESTORE_STAGING_ROOT" ]; then
  log_info "--- Offline target structural baseline ---"
  if [ -n "$ISA_UID" ]; then
    log_info "Target service identity: trusted explicit UID:GID $ISA_UID:$ISA_GID (never the installer host's own /etc/passwd lookup for '$ISA_USER')."
  else
    log_info "Target service identity '$ISA_USER' will be resolved from $RESTORE_STAGING_ROOT/etc/passwd, not the installer host -- pass --isa-uid/--isa-gid if this target has no /etc/passwd of its own."
  fi
  if "$VENV_PY" manage.py check_deploy_baseline \
      --structural-only \
      --target-root "$RESTORE_STAGING_ROOT" \
      "${CHECK_BASELINE_IDENTITY_ARGS[@]}"; then
    log_info "offline target check_deploy_baseline: PASS"
    log_info "95-validate: PASS (offline target structural/filesystem contract; live DB, station, kernel, and runtime execution checks intentionally deferred until boot-root validation)"
    exit 0
  fi
  log_error "offline target check_deploy_baseline: FAILED -- the staged filesystem is incomplete or unsafe, or the target service identity could not be resolved (no target /etc/passwd entry for '$ISA_USER' and no --isa-uid/--isa-gid supplied). Installer-host state cannot satisfy this check."
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
# full PASS/FAIL/UNRESOLVED contract. --isa-user (and --isa-uid/--isa-gid,
# if this real install was deliberately given them) thread through the
# SAME identity this stage resolved above, so the TTS scratch-surface
# evidence can actually resolve rather than reporting unresolved. A real
# host normally never needs --isa-uid/--isa-gid: its own /etc/passwd
# already resolves --isa-user, exactly as before.
if "$VENV_PY" manage.py check_deploy_baseline "${CHECK_BASELINE_IDENTITY_ARGS[@]}"; then
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
