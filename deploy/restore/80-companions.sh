#!/usr/bin/env bash
# deploy/restore/80-companions.sh -- IsadoraAir 1.2 Phase 4.
#
# Clones + provisions the three companion repos (syndicated-ingest,
# weather-ingest, ogremote-ingest -- all private, IsadoraAir 1.2
# Phase 2B) at their expected paths, each with its own venv (no
# --system-site-packages -- none of them touch GStreamer) and its own
# Phase 3 requirements.txt.
#
# GitHub access to these private repos is an external provisioning
# requirement -- this script never embeds credentials; it relies on
# GIT_SSH_COMMAND/ssh-agent/whatever the operator has already set up
# being usable non-interactively, exactly like Phase 2B's own push
# workflow did. See docs/DISASTER_RECOVERY_RESTORE.md's "Manual
# checkpoints" for the "obtain GitHub private-repo access" item.
#
# Per-project runtime directories/credentials this script does NOT
# fabricate (documented, not invented -- Phase 4 spec section 20):
#   syndicated-ingest  ~/.syndicated_ingest.cred
#   ogremote-ingest     ~/.ogremote_ingest.cred, data/ (untracked state)
#   weather-ingest      NO standalone cred file -- config lives in
#                        IsadoraAir's own database (WeatherConfig/
#                        AmberAlertConfig), read via
#                        $ISADORAAIR_DIR/venv/bin/python manage.py
#                        dump_weather_config -- IsadoraAir itself
#                        (stages 20/30/60) must already be restored and
#                        migrated before this project can do anything
#                        useful, per docs/RUNTIME_BASELINE.md's
#                        restore-order dependency map.
# `incoming/`-style directories with a tracked `.gitkeep` are recreated
# automatically by the clone itself -- nothing extra needed for those.
#
# Usage:
#   deploy/restore/80-companions.sh [--plan|--apply] [--staging-root PATH]
#     [--companions-root PATH] [--repo-url-prefix PREFIX]
#     [--only syndicated-ingest,weather-ingest,ogremote-ingest]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

restore_parse_common_args "$@"
set -- "${RESTORE_REMAINING_ARGS[@]}"

COMPANIONS_ROOT=""
REPO_URL_PREFIX="git@github.com:celltech161"
ONLY="syndicated-ingest,weather-ingest,ogremote-ingest"
while [ $# -gt 0 ]; do
  case "$1" in
    --companions-root) COMPANIONS_ROOT="${2:?}"; shift 2 ;;
    --companions-root=*) COMPANIONS_ROOT="${1#*=}"; shift ;;
    --repo-url-prefix) REPO_URL_PREFIX="${2:?}"; shift 2 ;;
    --repo-url-prefix=*) REPO_URL_PREFIX="${1#*=}"; shift ;;
    --only) ONLY="${2:?}"; shift 2 ;;
    --only=*) ONLY="${1#*=}"; shift ;;
    *) log_error "80-companions.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done
[ -z "$COMPANIONS_ROOT" ] && COMPANIONS_ROOT="${RESTORE_STAGING_ROOT:-$HOME}"

log_info "=== 80-companions ==="
guard_production_target
require_cmd git
require_cmd python3

IFS=',' read -ra REPOS <<< "$ONLY"

declare -A STATUS
for repo in "${REPOS[@]}"; do
  log_info "-- $repo --"
  TARGET="$COMPANIONS_ROOT/$repo"
  URL="$REPO_URL_PREFIX/$repo.git"

  if [ -d "$TARGET/.git" ]; then
    log_info "$TARGET already cloned -- fetching to verify remote is reachable, not re-cloning."
    do_or_plan git -C "$TARGET" fetch --all
    if [ "$RESTORE_MODE" = "apply" ]; then
      ACTUAL_REMOTE=$(git -C "$TARGET" remote get-url origin 2>/dev/null || echo "")
      if [ "$ACTUAL_REMOTE" != "$URL" ]; then
        log_warn "$TARGET's origin ($ACTUAL_REMOTE) does not match expected ($URL) -- not touching it, just noting the mismatch."
      fi
    fi
  elif [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
    log_error "$TARGET exists, is non-empty, and is not a Git checkout. Refusing to clone into it."
    STATUS[$repo]="ERROR"
    continue
  else
    do_or_plan mkdir -p "$COMPANIONS_ROOT"
    do_or_plan git clone "$URL" "$TARGET"
  fi

  REQUIREMENTS="$TARGET/requirements.txt"
  if [ "$RESTORE_MODE" = "apply" ] && [ ! -f "$REQUIREMENTS" ]; then
    log_error "$REQUIREMENTS not found -- expected every companion repo to have one as of IsadoraAir 1.2 Phase 3. Skipping venv setup for $repo."
    STATUS[$repo]="ERROR (no requirements.txt)"
    continue
  fi

  VENV_DIR="$TARGET/venv"
  if [ -d "$VENV_DIR" ]; then
    log_info "$VENV_DIR already exists -- skipping recreation."
  else
    do_or_plan python3 -m venv "$VENV_DIR"
  fi
  do_or_plan "$VENV_DIR/bin/pip" install --upgrade pip
  do_or_plan "$VENV_DIR/bin/pip" install -r "$REQUIREMENTS"

  if [ "$repo" = "ogremote-ingest" ]; then
    do_or_plan mkdir -p "$TARGET/data"
    log_info "ogremote-ingest: created data/ (untracked per-run state -- last_batch.json, urgent_pa_state.json)."
  fi

  if [ "$RESTORE_MODE" = "apply" ]; then
    log_info "$repo: cloned + venv built. Credential provisioning is a separate manual step (see this script's header)."
    STATUS[$repo]="PROVISIONED (code + venv only -- see credential note above)"
  fi
done

if [ "$RESTORE_MODE" = "apply" ]; then
  log_info "Companion provisioning summary:"
  for repo in "${REPOS[@]}"; do
    log_info "  $repo: ${STATUS[$repo]:-not attempted}"
  done
fi
log_info "80-companions: $( [ "$RESTORE_MODE" = apply ] && echo "PASS (see summary above)" || echo "PLAN complete" )"
