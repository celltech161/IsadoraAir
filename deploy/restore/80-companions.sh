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
# r0042: a requested companion that cannot actually be provisioned
# (non-Git collision, missing requirements.txt) used to log an ERROR
# and `continue` to the next repo, but nothing ever made THIS flag
# affect the stage's own exit code -- every apply run reached the
# unconditional "80-companions: PASS" below regardless. Never true for
# a whole-machine restore acceptance: a requested companion that never
# got cloned/provisioned must fail this stage closed. Manual credential
# provisioning remaining outstanding is NOT this -- see this script's
# own header and the PROVISIONED status below, deliberately not an
# error status.
ANY_FAILED=0
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
    # r0043: a confirmed real E8 defect left a non-empty, non-Git
    # $HOME/weather-ingest behind -- a LEGACY WEATHER_DATA_DIR restored
    # verbatim into .env pointed INSIDE this exact companion checkout
    # namespace, and Stage 60's first Django import (weather/services.py's
    # own module-level DATA_DIR.mkdir(parents=True, exist_ok=True))
    # manufactured the empty directory tree before this stage ever ran.
    # 40-station-content.sh now normalizes that root cause for every
    # FRESH restore (see that stage's own section 5) -- this handles the
    # machine that already has the pre-r0043 damage sitting on disk.
    #
    # Repaired ONLY when ALL of the following hold -- never a general
    # "trust any empty directory" mechanism, and never for any repo
    # other than weather-ingest (the one, specific, currently-known
    # defect this addresses):
    #   1. --resume was explicitly given.
    #   2. This is exactly weather-ingest, at exactly its default
    #      companions-root location (restore_default_companions_root)
    #      -- the SAME known legacy namespace 40-station-content.sh's
    #      own normalization check uses, never wherever --companions-
    #      root/--repo-url-prefix might point THIS invocation.
    #   3. The directory's entire recursive content is REAL directories
    #      only -- zero regular files, zero symlinks anywhere in the
    #      tree, no .git -- the exact, narrow signature Stage 60's own
    #      mkdir -p (and nothing else) can produce; a single real file
    #      anywhere disqualifies it immediately.
    #   4. Durable ledger evidence proves 40-station-content.sh's own
    #      normalization already ran, for THIS EXACT archive identity,
    #      against THIS EXACT target root -- i.e. this restore session
    #      (not a guess, not merely "some ledger exists somewhere")
    #      already fixed the root cause. restore_ledger_stage_state's
    #      own identity check separately fails closed (hard error) on
    #      a ledger recorded against a different archive/target.
    # Any other pre-existing content -- real files, a different repo,
    # no matching ledger provenance, or --resume not given -- falls
    # straight through to the unchanged, strict failure below.
    WEATHER_SCAFFOLD_REPAIRED=0
    if [ "$RESTORE_RESUME" -eq 1 ] && [ "$repo" = "weather-ingest" ] \
        && [ "$TARGET" = "$(restore_default_companions_root)/weather-ingest" ] \
        && _restore_is_known_empty_scaffold "$TARGET"; then
      NORMALIZATION_STAGE_STATE=$(restore_ledger_stage_state "40-station-content") || exit 1
      if [ "$NORMALIZATION_STAGE_STATE" = "complete" ]; then
        log_warn "$TARGET matches the known r0043 legacy-WEATHER_DATA_DIR scaffold signature (empty directory tree, no .git, no regular files anywhere) -- durable ledger evidence proves 40-station-content.sh's WEATHER_DATA_DIR normalization already ran for this exact archive/target, so this is provably restore-tooling-manufactured scaffold, not real operator content. Repairing (removing) it before cloning -- see docs/DISASTER_RECOVERY_STATUS.md."
        do_or_plan rm -rf "$TARGET"
        WEATHER_SCAFFOLD_REPAIRED=1
      else
        log_warn "$TARGET looks like the known r0043 legacy scaffold signature, but the ledger does not (yet) record 40-station-content.sh complete for this exact archive/target -- NOT repairing; falling through to the strict collision failure below."
      fi
    fi
    if [ "$WEATHER_SCAFFOLD_REPAIRED" -eq 1 ]; then
      do_or_plan mkdir -p "$COMPANIONS_ROOT"
      do_or_plan git clone "$URL" "$TARGET"
    else
      log_error "$TARGET exists, is non-empty, and is not a Git checkout. Refusing to clone into it."
      STATUS[$repo]="ERROR"
      ANY_FAILED=1
      continue
    fi
  else
    do_or_plan mkdir -p "$COMPANIONS_ROOT"
    do_or_plan git clone "$URL" "$TARGET"
  fi

  REQUIREMENTS="$TARGET/requirements.txt"
  if [ "$RESTORE_MODE" = "apply" ] && [ ! -f "$REQUIREMENTS" ]; then
    log_error "$REQUIREMENTS not found -- expected every companion repo to have one as of IsadoraAir 1.2 Phase 3. Skipping venv setup for $repo."
    STATUS[$repo]="ERROR (no requirements.txt)"
    ANY_FAILED=1
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
  if [ "$ANY_FAILED" -eq 1 ]; then
    log_error "80-companions: FAIL -- one or more requested companion repositories could not be provisioned (see summary above). Manual credential provisioning remaining outstanding is expected and does not count against this; a repo actually marked ERROR above does."
    exit 1
  fi
  restore_ledger_record "80-companions"
  log_info "80-companions: PASS (see summary above)"
else
  log_info "80-companions: PLAN complete"
fi
