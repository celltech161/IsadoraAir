#!/usr/bin/env bash
# deploy/restore/50-native-deps.sh -- IsadoraAir 1.2 Phase 4 / Runtime
# Foundation E7B.
#
# Two entirely separate modes, chosen automatically (never mixed):
#
#   Backup-based disaster recovery (--archive was given, and neither
#   --source-dir nor --download-sources was explicitly passed): locates
#   this restore's embedded Runtime Foundation E7 recovery payload (via
#   lib.sh's restore_locate_recovery_payload -- the one shared contract
#   stages 50/70 both use, see docs/DISASTER_RECOVERY_RESTORE.md),
#   validates it, then delegates to the REAL Runtime Foundation E4
#   authority (monitoring/management/commands/provision_runtime_components.py
#   --fdkaac, via --recovery-payload) for both the unprivileged prepare
#   phase and the protected publish phase -- always THIS checkout's own
#   copy of that authority, via lib.sh's restore_manage, never the
#   restored backup's own possibly-older copy (Runtime Foundation E7C --
#   see restore_manage.py's own docstring for the full "recovery source
#   authority vs. restored target" split). This stage does not build
#   anything itself, does not re-implement E4's verification, and NEVER
#   reaches for --download-sources -- a legacy/v2.x or explicitly non-
#   self-contained archive fails this backup-based stage plainly rather
#   than silently falling back to the network (Runtime Foundation E7B
#   task step 16 -- see "Backward compatibility" in
#   docs/DISASTER_RECOVERY_RESTORE.md).
#
#   Explicit connected/fresh install (--source-dir, --download-sources,
#   or no --archive at all): UNCHANGED from Phase 4 -- delegates
#   straight to deploy/build_fdkaac.sh, exactly as before. This is a
#   deliberate, separate, operator-selected concern, not a fallback a
#   backup-based restore ever reaches for on its own (task step 14).
#
# Foundation E4's real prepare/publish split needs a Django environment
# to run as a manage.py command -- which is why, for the recovery-payload
# path only, this stage now depends on 60-python.sh having already
# created $RESTORE_TARGET_ROOT/venv. This is a REAL new dependency
# Runtime Foundation E7B introduces (native fdkaac's old direct C build
# had none); restore.sh's stage order was updated to run 60-python before
# 50-native-deps to match -- see deploy/restore/README.md's "Restore-
# order dependency map" for the 2026-08-29 note, and 60-python.sh's own
# idempotence guarantee (safe to have already run, or to run again later
# at its usual numeric spot -- it verifies rather than recreates). Note
# the split (Runtime Foundation E7C): that venv only supplies the Python
# INTERPRETER, verified compatible with this checkout's requirements.txt
# first -- the manage.py command it runs always comes from this checkout.
# The legacy connected-install path below has no such dependency and is
# unaffected.
#
# Usage:
#   deploy/restore/50-native-deps.sh --archive PATH [--plan|--apply]
#     [--staging-root PATH] [--trusted-preparer-uid UID]
#   deploy/restore/50-native-deps.sh [--plan|--apply] [--staging-root PATH]
#     [--prefix PATH] [--jobs N] [--source-dir PATH | --download-sources]
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
TRUSTED_PREPARER_UID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="${2:?--prefix needs a path}"; shift 2 ;;
    --prefix=*) PREFIX="${1#*=}"; shift ;;
    --jobs) JOBS="${2:?--jobs needs a number}"; shift 2 ;;
    --jobs=*) JOBS="${1#*=}"; shift ;;
    --source-dir) SOURCE_DIR="${2:?--source-dir needs a path}"; shift 2 ;;
    --source-dir=*) SOURCE_DIR="${1#*=}"; shift ;;
    --download-sources) DOWNLOAD_SOURCES=1; shift ;;
    --trusted-preparer-uid) TRUSTED_PREPARER_UID="${2:?--trusted-preparer-uid needs a UID}"; shift 2 ;;
    --trusted-preparer-uid=*) TRUSTED_PREPARER_UID="${1#*=}"; shift ;;
    *) log_error "50-native-deps.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done

if [ -n "$SOURCE_DIR" ] && [ "$DOWNLOAD_SOURCES" -eq 1 ]; then
  log_error "Choose --source-dir or --download-sources, not both."
  exit 2
fi

log_info "=== 50-native-deps (HE-AAC/fdkaac) ==="
guard_production_target

# ---- Mode selection --------------------------------------------------
# Backup-based DR is the default whenever an --archive is present and
# the operator did not explicitly ask for the legacy connected path --
# never the other way around (an explicit --source-dir/--download-sources
# always wins, even alongside --archive, since that is an unambiguous
# operator choice).
USE_RECOVERY_PAYLOAD=0
if [ -n "$RESTORE_ARCHIVE" ] && [ -z "$SOURCE_DIR" ] && [ "$DOWNLOAD_SOURCES" -eq 0 ]; then
  USE_RECOVERY_PAYLOAD=1
fi

if [ "$USE_RECOVERY_PAYLOAD" -eq 1 ]; then
  # =====================================================================
  # Backup-based disaster recovery: Runtime Foundation E7B payload path.
  # =====================================================================
  require_cmd tar

  # E4's canonical target root ("/usr/local/...", mapped) is NOT the
  # same thing as $RESTORE_TARGET_ROOT (the application root,
  # "/opt/isadoraair" or "$STAGING_ROOT/opt/isadoraair") -- see this
  # file's header. Staging: publish beneath the whole staging root, so
  # it lands at $RESTORE_STAGING_ROOT/usr/local/... . Real restore:
  # literal / -- the real canonical location -- which the E4 CLI itself
  # then correctly refuses without root and --trusted-preparer-uid; this
  # script does not weaken that.
  NATIVE_TARGET_ROOT="${RESTORE_STAGING_ROOT:-/}"
  log_info "Native fdkaac (E4) target root: $NATIVE_TARGET_ROOT"

  if [ "$RESTORE_MODE" != "apply" ]; then
    log_plan "locate + validate the runtime-recovery/ payload embedded in $RESTORE_ARCHIVE"
    log_plan "restore_manage provision_runtime_components --fdkaac --prepare-fdkaac --recovery-payload <payload>/native/fdkaac --prepared-native-root <tmp> --target-root $NATIVE_TARGET_ROOT"
    log_plan "restore_manage provision_runtime_components --fdkaac --publish-fdkaac --recovery-payload <payload>/native/fdkaac --prepared-native-root <tmp> --target-root $NATIVE_TARGET_ROOT${TRUSTED_PREPARER_UID:+ --trusted-preparer-uid $TRUSTED_PREPARER_UID}"
    log_info "50-native-deps: PLAN complete"
    exit 0
  fi

  # restore_manage (lib.sh) owns the venv-python and .env preconditions
  # (and, before running anything, whether that venv is even compatible
  # with this checkout's requirements.txt) with one shared, clear
  # diagnostic -- this stage only needs its own ordering precondition:
  # has 20-application.sh actually reconstructed the target checkout yet.
  if [ ! -f "$RESTORE_TARGET_ROOT/manage.py" ]; then
    log_error "$RESTORE_TARGET_ROOT/manage.py not found -- run 20-application.sh first."
    exit 1
  fi

  WORKDIR="$(mktemp -d /tmp/isadoraair-restore-native-recovery.XXXXXX)"
  cleanup_native_recovery() { rm -rf "$WORKDIR"; }
  trap cleanup_native_recovery EXIT
  PAYLOAD_DIR="$WORKDIR/payload"
  PREPARED_DIR="$WORKDIR/prepared"

  restore_locate_recovery_payload "$PAYLOAD_DIR"
  if [ "$RESTORE_RECOVERY_PAYLOAD_FOUND" -ne 1 ]; then
    log_error "LEGACY ARCHIVE -- NOT SELF-CONTAINED FOR FOUNDATION E. Backup-based native recovery fails closed and never falls back to --download-sources. For an old archive, deliberately run the documented connected/manual path with --source-dir or --download-sources."
    exit 1
  fi

  log_apply "restore_manage validate_runtime_recovery_payload $PAYLOAD_DIR --json"
  RECOVERY_EVIDENCE_JSON=$(restore_manage validate_runtime_recovery_payload "$PAYLOAD_DIR" --json)
  NATIVE_STATE=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["components"]["native_fdkaac"]["state"])' "$RECOVERY_EVIDENCE_JSON")
  if [ "$NATIVE_STATE" != "present" ]; then
    log_info "50-native-deps: no native_fdkaac component is included; no native recovery action is required by this archive"
    exit 0
  fi

  log_apply "restore_manage provision_runtime_components --fdkaac --prepare-fdkaac --recovery-payload $PAYLOAD_DIR --prepared-native-root $PREPARED_DIR --target-root $NATIVE_TARGET_ROOT"
  restore_manage provision_runtime_components \
      --fdkaac --prepare-fdkaac \
      --recovery-payload "$PAYLOAD_DIR" \
      --prepared-native-root "$PREPARED_DIR" \
      --target-root "$NATIVE_TARGET_ROOT"

  PUBLISH_ARGS=(--fdkaac --publish-fdkaac --recovery-payload "$PAYLOAD_DIR" --prepared-native-root "$PREPARED_DIR" --target-root "$NATIVE_TARGET_ROOT")
  if [ -n "$TRUSTED_PREPARER_UID" ]; then
    PUBLISH_ARGS+=(--trusted-preparer-uid "$TRUSTED_PREPARER_UID")
  fi
  log_apply "restore_manage provision_runtime_components ${PUBLISH_ARGS[*]}"
  restore_manage provision_runtime_components "${PUBLISH_ARGS[@]}"

  restore_record_recovery_components native_fdkaac >/dev/null

  log_info "50-native-deps: PASS (native fdkaac recovered from the Runtime Foundation E7 payload via Foundation E4's real prepare/publish authority)"
  exit 0
fi

# =========================================================================
# Legacy / explicit connected-install path -- UNCHANGED from Phase 4.
# =========================================================================
if [ -z "$SOURCE_DIR" ] && [ "$DOWNLOAD_SOURCES" -eq 0 ]; then
  DOWNLOAD_SOURCES=1
  log_warn "No local fdkaac source directory supplied; using explicit connected-install acquisition. A DR restore should pass --source-dir or FDKAAC_SOURCE_DIR and never reaches the network."
fi

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
