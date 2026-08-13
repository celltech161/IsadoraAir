#!/usr/bin/env bash
# deploy/restore/50-native-deps.sh -- IsadoraAir 1.2 Phase 4.
#
# Wraps Phase 3's own deploy/build_fdkaac.sh + deploy/check_he_aac.sh
# into the ordered restore flow: install build prerequisites (handled by
# 10-packages.sh's BUILD_HEAAC group, verified again here defensively),
# build the pinned fdk-aac/fdkaac, run the real LC/HE/HEv2 smoke checks,
# fail before encoder bring-up if support is missing.
#
# PREFIX defaults to $RESTORE_TARGET_ROOT/native/fdkaac -- NEVER
# /usr/local -- matching build_fdkaac.sh's own safety rule that
# /usr/local is only ever a deliberate, separate, explicitly-approved
# step. Pass --prefix /usr/local yourself, outside --staging-root, as
# that separate step when actually deploying to a real box (Phase 5) --
# this stage will not default there under any combination of
# --apply/--staging-root.
#
# Usage:
#   deploy/restore/50-native-deps.sh [--plan|--apply] [--staging-root PATH]
#     [--prefix PATH] [--jobs N]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

restore_parse_common_args "$@"
set -- "${RESTORE_REMAINING_ARGS[@]}"

PREFIX=""
JOBS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix needs a path}"; shift 2 ;;
    --prefix=*) PREFIX="${1#*=}"; shift ;;
    --jobs) JOBS="${2:?--jobs needs a number}"; shift 2 ;;
    --jobs=*) JOBS="${1#*=}"; shift ;;
    *) log_error "50-native-deps.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done

log_info "=== 50-native-deps (HE-AAC/fdkaac) ==="
guard_production_target

if [ -z "$PREFIX" ]; then
  if [ -n "$RESTORE_STAGING_ROOT" ]; then
    PREFIX="$RESTORE_STAGING_ROOT/native/fdkaac"
  else
    PREFIX="$RESTORE_TARGET_ROOT/../native/fdkaac"
    log_warn "No --prefix given for a non-staging run -- defaulting to $PREFIX (NOT /usr/local). Pass --prefix /usr/local explicitly, as its own deliberate step, to install where production actually looks (see build_fdkaac.sh's own header comment)."
  fi
fi
log_info "PREFIX: $PREFIX"

BUILD_SCRIPT="$REPO_ROOT/deploy/build_fdkaac.sh"
CHECK_SCRIPT="$REPO_ROOT/deploy/check_he_aac.sh"
if [ ! -x "$BUILD_SCRIPT" ] || [ ! -x "$CHECK_SCRIPT" ]; then
  log_error "Missing or non-executable: $BUILD_SCRIPT / $CHECK_SCRIPT"
  exit 1
fi

# ---- Build prerequisites (defensive re-check; 10-packages.sh's
#      BUILD_HEAAC group should already cover this). ---------------------
MISSING_TOOLS=()
for cmd in git gcc g++ make autoconf automake libtoolize pkg-config; do
  command -v "$cmd" >/dev/null 2>&1 || MISSING_TOOLS+=("$cmd")
done
if [ "${#MISSING_TOOLS[@]}" -gt 0 ]; then
  log_error "Missing build tools: ${MISSING_TOOLS[*]} -- run 10-packages.sh first (BUILD_HEAAC group)."
  exit 1
fi

if [ "$RESTORE_MODE" = "apply" ]; then
  log_apply "PREFIX=$PREFIX${JOBS:+ JOBS=$JOBS} bash $BUILD_SCRIPT"
  if [ -n "$JOBS" ]; then
    PREFIX="$PREFIX" JOBS="$JOBS" bash "$BUILD_SCRIPT"
  else
    PREFIX="$PREFIX" bash "$BUILD_SCRIPT"
  fi
  log_info "Build complete. Running HE-AAC/HEv2 capability smoke check..."
  if "$CHECK_SCRIPT" "$PREFIX/bin/fdkaac" "$PREFIX/lib"; then
    log_info "HE-AAC/HEv2 capability check: PASS"
  else
    log_error "HE-AAC/HEv2 capability check FAILED -- built binary does not support the required profiles. Refusing to consider this stage complete; do not proceed to encoder bring-up with this build."
    exit 1
  fi
  log_info "50-native-deps: PASS (built + verified at $PREFIX)"
else
  log_plan "PREFIX=$PREFIX${JOBS:+ JOBS=$JOBS} bash $BUILD_SCRIPT"
  log_plan "$CHECK_SCRIPT $PREFIX/bin/fdkaac $PREFIX/lib (LC/HE/HEv2 smoke check)"
  log_info "50-native-deps: PLAN complete"
fi
