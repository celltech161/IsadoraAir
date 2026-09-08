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
#     [--staging-root PATH]
#   deploy/restore/50-native-deps.sh [--plan|--apply] [--staging-root PATH]
#     [--prefix PATH] [--jobs N] [--source-dir PATH | --download-sources]
#
# r0041: --trusted-preparer-uid used to be an external flag this stage
# only forwarded on request -- a real canonical (non-staging) restore
# left it unset, so provision_runtime_components' own publish phase
# correctly refused ("canonical native publication requires
# --trusted-preparer-uid"), a real E8 Stage-50 failure. Removed as a
# public option: this stage always runs BOTH the unprivileged prepare
# phase and the privileged canonical publish phase in the same
# invocation, so it can -- and now does -- determine the real preparer
# UID itself (whatever this process's own UID was when it ran prepare),
# never accepting an operator-supplied value a bare-machine operator
# would have no way to know is correct, and which an external caller
# supplying an arbitrary UID could otherwise use to bypass
# NativeRuntimeProvisioner.publish()'s ownership check entirely. A
# canonical restore now runs unprivileged prepare, then ONLY the
# publish half under sudo, passing that captured UID -- see
# lib.sh's restore_manage_command and 75-protected-updater.sh's
# established USE_SUDO idiom, the same pattern used here.
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
  # it lands at $RESTORE_STAGING_ROOT/usr/local/... -- unprivileged
  # throughout, matching 75-protected-updater.sh's own USE_SUDO=0 case.
  # Real restore: literal / -- the real canonical location -- which
  # NativeRuntimeProvisioner.publish() requires root for; this script
  # runs ONLY that publish half under sudo (never prepare -- see below),
  # matching 75-protected-updater.sh's own established USE_SUDO=1 idiom.
  if [ -n "$RESTORE_STAGING_ROOT" ]; then
    NATIVE_TARGET_ROOT="$RESTORE_STAGING_ROOT"
    USE_SUDO=0
  else
    NATIVE_TARGET_ROOT="/"
    USE_SUDO=1
  fi
  log_info "Native fdkaac (E4) target root: $NATIVE_TARGET_ROOT"

  if [ "$RESTORE_MODE" != "apply" ]; then
    log_plan "locate + validate the runtime-recovery/ payload embedded in $RESTORE_ARCHIVE"
    log_plan "restore_manage provision_runtime_components --fdkaac --prepare-fdkaac --recovery-payload <payload>/native/fdkaac --prepared-native-root <tmp> --target-root $NATIVE_TARGET_ROOT (unprivileged)"
    if [ "$USE_SUDO" -eq 1 ]; then
      log_plan "sudo restore_manage provision_runtime_components --fdkaac --publish-fdkaac --recovery-payload <payload>/native/fdkaac --prepared-native-root <tmp> --target-root $NATIVE_TARGET_ROOT --trusted-preparer-uid <uid this process prepared as>"
    else
      log_plan "restore_manage provision_runtime_components --fdkaac --publish-fdkaac --recovery-payload <payload>/native/fdkaac --prepared-native-root <tmp> --target-root $NATIVE_TARGET_ROOT (unprivileged)"
    fi
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
    restore_ledger_record "50-native-deps"
    log_info "50-native-deps: no native_fdkaac component is included; no native recovery action is required by this archive"
    exit 0
  fi

  # ---- Resume/adopt: r0043/r0044. publish_phase_d_component's sibling
  #      in Foundation E4 (NativeRuntimeProvisioner.publish) also
  #      refuses to silently overwrite pre-existing destination content
  #      -- a real prior publish of this exact component (whether
  #      ledger-recorded already, or pre-ledger) is therefore NOT simply
  #      safe to blindly re-run. Both cases below share the SAME
  #      receipt-based evidence -- see 75-protected-updater.sh's own
  #      comment for the full "complete" (fail closed on disagreement)
  #      vs "absent + --adopt-pre-ledger" (fall through, normal publish
  #      is its own fail-closed backstop) contract.
  if [ "$RESTORE_RESUME" -eq 1 ]; then
    LEDGER_STAGE_STATE=$(restore_ledger_stage_state "50-native-deps") || exit 1
    if [ "$LEDGER_STAGE_STATE" = "complete" ]; then
      log_info "50-native-deps: --resume -- ledger records this stage already complete for this exact archive/target; verifying the existing runtime-recovery receipt rather than re-publishing."
      if restore_verify_component_receipt native_fdkaac >/dev/null 2>&1; then
        log_info "50-native-deps: resume verification PASS -- the existing runtime-recovery receipt still proves native_fdkaac was recovered from this exact archive/payload. Not re-publishing."
        restore_ledger_record "50-native-deps"
        log_info "50-native-deps: PASS (resumed/verified)"
        exit 0
      fi
      log_error "50-native-deps: resume verification FAILED -- the ledger records this stage already complete, but the runtime-recovery receipt no longer proves native_fdkaac was recovered from this exact archive/payload. Ledger and receipt disagree -- refusing to guess which is authoritative; investigate manually (or remove the stale ledger entry at $(restore_ledger_path)) before retrying."
      exit 1
    elif [ "$LEDGER_STAGE_STATE" = "absent" ] && [ "$RESTORE_ADOPT_PRE_LEDGER" -eq 1 ]; then
      log_info "50-native-deps: --resume --adopt-pre-ledger -- no ledger entry exists yet for this stage; checking whether the existing runtime-recovery receipt already proves native_fdkaac was recovered from this exact archive."
      if restore_verify_component_receipt native_fdkaac >/dev/null 2>&1; then
        log_info "50-native-deps: adoption verification PASS -- the existing runtime-recovery receipt already proves native_fdkaac was recovered from this exact archive/payload. Adopting -- recording this stage complete without re-publishing."
        restore_ledger_record "50-native-deps" --detail '{"adopted":true}'
        log_info "50-native-deps: PASS (adopted)"
        exit 0
      fi
      log_info "50-native-deps: adoption not provable from the existing receipt (or none exists) -- proceeding with the normal prepare/publish below (which itself refuses to overwrite any real pre-existing destination content)."
    fi
  fi

  # Unprivileged, always -- this is the E4 trust handoff's whole point
  # (prepare as an ordinary user; only publish is ever privileged).
  # Never wrapped in sudo, canonical target or not.
  log_apply "restore_manage provision_runtime_components --fdkaac --prepare-fdkaac --recovery-payload $PAYLOAD_DIR --prepared-native-root $PREPARED_DIR --target-root $NATIVE_TARGET_ROOT"
  restore_manage provision_runtime_components \
      --fdkaac --prepare-fdkaac \
      --recovery-payload "$PAYLOAD_DIR" \
      --prepared-native-root "$PREPARED_DIR" \
      --target-root "$NATIVE_TARGET_ROOT"
  # The UID that ACTUALLY just prepared the tree above -- this process's
  # own, captured immediately after prepare succeeds. Never an
  # externally-supplied value: publish's own ownership check
  # (NativeRuntimeProvisioner.publish -> _validated_preparer_uid) exists
  # specifically so an arbitrary/wrong UID can never be trusted, and a
  # bare-machine operator has no legitimate way to know the right value
  # except by asking this same process what it just did.
  PREPARER_UID="$(id -u)"

  PUBLISH_ARGS=(--fdkaac --publish-fdkaac --recovery-payload "$PAYLOAD_DIR" --prepared-native-root "$PREPARED_DIR" --target-root "$NATIVE_TARGET_ROOT")
  if [ "$USE_SUDO" -eq 1 ]; then
    PUBLISH_ARGS+=(--trusted-preparer-uid "$PREPARER_UID")
  fi
  # restore_manage_command (not the plain restore_manage wrapper) here --
  # a real (non-staging) publish needs the whole invocation, venv python
  # included, run under sudo; bash functions aren't visible to a separate
  # sudo process, but a resolved argv is. Matches
  # 75-protected-updater.sh's own established real-root publish pattern.
  restore_manage_command provision_runtime_components "${PUBLISH_ARGS[@]}"
  if [ "$USE_SUDO" -eq 1 ]; then
    RESTORE_MANAGE_CMD=(sudo "${RESTORE_MANAGE_CMD[@]}")
  fi
  log_apply "${RESTORE_MANAGE_CMD[*]}"
  "${RESTORE_MANAGE_CMD[@]}"

  restore_record_recovery_components native_fdkaac >/dev/null

  restore_ledger_record "50-native-deps"
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
  restore_ledger_record "50-native-deps"
  log_info "50-native-deps: PASS (built + linkage/capability verified at $PREFIX)"
else
  log_plan "$BUILD_SCRIPT ${BUILD_ARGS[*]}"
  log_plan "Build script validates version, intended linkage, LC, HE/SBR, HEv2/SBR+PS, and ffmpeg decode"
  log_info "50-native-deps: PLAN complete"
fi
