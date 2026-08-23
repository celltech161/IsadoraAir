#!/usr/bin/env bash
# deploy/restore/50-native-deps.sh -- IsadoraAir 1.2 Phase 4.
#
# Delegates to deploy/build_fdkaac.sh's one archive-driven build path. A DR
# restore supplies --source-dir (or FDKAAC_SOURCE_DIR) and therefore never
# needs GitHub. Optional --download-sources is only for a connected fresh
# install. The build script itself performs linkage + LC/HE/HEv2 validation.
#
# PREFIX defaults to $RESTORE_TARGET_ROOT/native/fdkaac -- NEVER
# /usr/local -- matching build_fdkaac.sh's own safety rule that
# /usr/local is only ever a deliberate, separate, explicitly-approved
# step. A production install invokes build_fdkaac.sh directly with both
# --prefix /usr/local and --allow-production-prefix; this restore wrapper
# neither defaults to nor bypasses that second guard.
#
# Usage:
#   deploy/restore/50-native-deps.sh [--plan|--apply] [--staging-root PATH]
#     [--prefix PATH] [--jobs N]
#     [--source-dir PATH | --download-sources]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

restore_parse_common_args "$@"
set -- "${RESTORE_REMAINING_ARGS[@]}"

PREFIX=""
JOBS=""
SOURCE_DIR="${FDKAAC_SOURCE_DIR:-}"
DOWNLOAD_SOURCES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix needs a path}"; shift 2 ;;
    --prefix=*) PREFIX="${1#*=}"; shift ;;
    --jobs) JOBS="${2:?--jobs needs a number}"; shift 2 ;;
    --jobs=*) JOBS="${1#*=}"; shift ;;
    --source-dir) SOURCE_DIR="${2:?--source-dir needs a path}"; shift 2 ;;
    --source-dir=*) SOURCE_DIR="${1#*=}"; shift ;;
    --download-sources) DOWNLOAD_SOURCES=1; shift ;;
    *) log_error "50-native-deps.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done

if [ -n "$SOURCE_DIR" ] && [ "$DOWNLOAD_SOURCES" -eq 1 ]; then
  log_error "Choose --source-dir or --download-sources, not both."
  exit 2
fi
if [ -z "$SOURCE_DIR" ] && [ "$DOWNLOAD_SOURCES" -eq 0 ]; then
  DOWNLOAD_SOURCES=1
  log_warn "No local fdkaac source directory supplied; using explicit connected-install acquisition. A DR restore should pass --source-dir or FDKAAC_SOURCE_DIR and never reaches the network."
fi

log_info "=== 50-native-deps (HE-AAC/fdkaac) ==="
guard_production_target

if [ -z "$PREFIX" ]; then
  if [ -n "$RESTORE_STAGING_ROOT" ]; then
    PREFIX="$RESTORE_STAGING_ROOT/native/fdkaac"
  else
    PREFIX="$RESTORE_TARGET_ROOT/../native/fdkaac"
    log_warn "No --prefix given for a non-staging run -- defaulting to $PREFIX (NOT /usr/local). A production install is a separate direct build_fdkaac.sh invocation with its second production-prefix guard."
  fi
fi
log_info "PREFIX: $PREFIX"

BUILD_SCRIPT="$REPO_ROOT/deploy/build_fdkaac.sh"
if [ ! -x "$BUILD_SCRIPT" ]; then
  log_error "Missing or non-executable: $BUILD_SCRIPT"
  exit 1
fi

# ---- Build prerequisites (defensive re-check; 10-packages.sh's
#      BUILD_HEAAC group should already cover this). ---------------------
MISSING_TOOLS=()
for cmd in gcc g++ make autoconf automake libtoolize pkg-config tar sha256sum readelf ldd ffmpeg; do
  command -v "$cmd" >/dev/null 2>&1 || MISSING_TOOLS+=("$cmd")
done
if [ "$DOWNLOAD_SOURCES" -eq 1 ] && ! command -v curl >/dev/null 2>&1; then
  MISSING_TOOLS+=("curl")
fi
if [ "${#MISSING_TOOLS[@]}" -gt 0 ]; then
  log_error "Missing build tools: ${MISSING_TOOLS[*]} -- run 10-packages.sh first (BUILD_HEAAC group)."
  exit 1
fi

SOURCE_ARGS=()
if [ -n "$SOURCE_DIR" ]; then
  SOURCE_ARGS=(--source-dir "$SOURCE_DIR")
  log_info "Source mode: local immutable archives at $SOURCE_DIR (network disabled)"
else
  SOURCE_ARGS=(--download-sources)
  log_info "Source mode: optional network acquisition with manifest hash verification"
fi

BUILD_ARGS=("${SOURCE_ARGS[@]}" --prefix "$PREFIX")
if [ -n "$JOBS" ]; then
  BUILD_ARGS+=(--jobs "$JOBS")
fi

if [ "$RESTORE_MODE" = "apply" ]; then
  log_apply "$BUILD_SCRIPT ${BUILD_ARGS[*]}"
  "$BUILD_SCRIPT" "${BUILD_ARGS[@]}"
  log_info "50-native-deps: PASS (built + linkage/capability verified at $PREFIX)"
else
  log_plan "$BUILD_SCRIPT ${BUILD_ARGS[*]}"
  log_plan "Build script validates version, intended linkage, LC, HE/SBR, HEv2/SBR+PS, and ffmpeg decode"
  log_info "50-native-deps: PLAN complete"
fi
