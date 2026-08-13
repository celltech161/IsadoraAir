#!/usr/bin/env bash
# deploy/restore/40-station-content.sh -- IsadoraAir 1.2 Phase 4.
#
# Reconstructs /srv/isadoraair's subtree structure, per
# docs/DISASTER_RECOVERY.md's own three-way classification:
#
#   Restored from the backup archive (srv-content/ inside it):
#     carts/         FXCart audio -- DB rows reference these files directly.
#     voicetracks/   recorded VT audio -- same, irreplaceable.
#
#   Recreated empty (regenerable / transient, never backed up):
#     waveforms/     rebuilt by `manage.py analyze_tracks` from the (restored)
#                    library catalog + the (NOT restored -- see below) audio.
#     aircheck/      continuous on-air recording, own retention policy.
#     rip_staging/   CD-rip working directory, cleared once processing completes.
#
#   External storage-resilience concern, NEVER touched by this script,
#   under ANY flag (see lib.sh's guard_never_touch_music_library --
#   there is no override):
#     music/         717+ GB library. This script creates the EMPTY
#                    mountpoint directory only (so a fresh install has
#                    somewhere for the real media to be attached/mounted
#                    later) -- it never writes a single audio file there.
#
# Mount setup for the real disk this directory is meant to sit on is a
# manual step -- see docs/DISASTER_RECOVERY_RESTORE.md's "Persistent
# storage mount" section (generic guidance + the current Oak Grove
# UUID/fstab entry as a reference example, not a generic default).
#
# Also restores two things outside /srv/isadoraair entirely, since they
# share this stage's "small, operator/durable content from the backup"
# character:
#   - REPORTS_ROOT (default /var/lib/isadoraair/reports) -- SoundExchange/
#     royalty filings, classified backup-required (not regenerable) per
#     docs/DISASTER_RECOVERY.md's "Reports" section.
#   - StereoTool's *.sts processing profile(s) -- restored to
#     --stereotool-dir (default $HOME/stereotool, matching
#     deploy/backup_isadoraair.sh's own STEREOTOOL_DIR default), plus a
#     printed manual-checkpoint checklist per Phase 4 spec section 16:
#     StereoTool's binary/license are NEVER part of this backup or repo
#     (proprietary, externally reprovisioned) -- restoring the profile
#     alone does not mean StereoTool is ready to run.
#
# Usage:
#   deploy/restore/40-station-content.sh --archive PATH [--plan|--apply]
#     [--staging-root PATH] [--owner USER:GROUP] [--stereotool-dir PATH]
#     [--stereotool-bin PATH]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

restore_parse_common_args "$@"
set -- "${RESTORE_REMAINING_ARGS[@]}"

OWNER="$(id -un):$(id -gn)"
STEREOTOOL_DIR="$HOME/stereotool"
STEREOTOOL_BIN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --owner) OWNER="${2:?--owner needs USER:GROUP}"; shift 2 ;;
    --owner=*) OWNER="${1#*=}"; shift ;;
    --stereotool-dir) STEREOTOOL_DIR="${2:?--stereotool-dir needs a path}"; shift 2 ;;
    --stereotool-dir=*) STEREOTOOL_DIR="${1#*=}"; shift ;;
    --stereotool-bin) STEREOTOOL_BIN="${2:?--stereotool-bin needs a path}"; shift 2 ;;
    --stereotool-bin=*) STEREOTOOL_BIN="${1#*=}"; shift ;;
    *) log_error "40-station-content.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done
if [ -n "$RESTORE_STAGING_ROOT" ]; then
  STEREOTOOL_DIR="$RESTORE_STAGING_ROOT/stereotool"
fi

log_info "=== 40-station-content ==="
guard_production_target
require_cmd tar

if [ -z "$RESTORE_ARCHIVE" ] || [ ! -f "$RESTORE_ARCHIVE" ]; then
  log_error "No valid --archive given. Run 00-preflight.sh first."
  exit 1
fi

# ---- Resolve SRV_ROOT: under --staging-root, a parallel tree under the
#      staging prefix; for a real restore, the real /srv/isadoraair.
#      Either way, LIBRARY_ROOT (music/) is computed the SAME way so
#      guard_never_touch_music_library's path match is reliable. --------
if [ -n "$RESTORE_STAGING_ROOT" ]; then
  SRV_ROOT="$RESTORE_STAGING_ROOT/srv/isadoraair"
else
  SRV_ROOT="/srv/isadoraair"
fi
log_info "Station-content root: $SRV_ROOT (owner: $OWNER)"

ensure_dir() {
  local path="$1"
  guard_never_touch_music_library "$path"
  if [ -n "$RESTORE_STAGING_ROOT" ]; then
    do_or_plan mkdir -p "$path"
  else
    do_or_plan sudo mkdir -p "$path"
    do_or_plan sudo chown "$OWNER" "$path"
  fi
}

# ---- 1. Restored-from-backup subtrees ------------------------------------
LISTING=$(tar -tzf "$RESTORE_ARCHIVE" 2>&1)
for sub in carts voicetracks; do
  ensure_dir "$SRV_ROOT/$sub"
  if grep -qE "^(\./)?srv-content/${sub}/" <<< "$LISTING"; then
    if [ "$RESTORE_MODE" = "apply" ]; then
      log_apply "restoring srv-content/$sub -> $SRV_ROOT/$sub"
      # srv-content/$sub is a REAL directory of ordinary files inside the
      # outer archive (backup_isadoraair.sh does a plain `cp -a` into its
      # workdir before the single final `tar czf`) -- NOT a nested
      # tar.gz like app.tar.gz is. A single ordinary extraction with
      # --strip-components is the right tool, not the extract-to-stdout-
      # then-re-untar trick app.tar.gz needs. --strip-components=2
      # removes the archive's own "./" + "srv-content" prefix, landing
      # "carts/whatever" (etc) directly under $SRV_ROOT. Verified against
      # a real archive during this stage's own Phase 4 validation.
      tar -xzf "$RESTORE_ARCHIVE" -C "$SRV_ROOT" --strip-components=2 "./srv-content/$sub"
      if [ -z "$RESTORE_STAGING_ROOT" ]; then
        sudo chown -R "$OWNER" "$SRV_ROOT/$sub"
      fi
      log_info "$sub: restored from backup."
    else
      log_plan "tar -xzf <archive> -C $SRV_ROOT --strip-components=2 ./srv-content/$sub"
    fi
  else
    log_warn "$sub: archive has no srv-content/$sub entries -- directory created empty (may be legitimate, e.g. a station with no operator-recorded content yet)."
  fi
done

# ---- 2. Recreated-empty (regenerable/transient) subtrees -----------------
for sub in waveforms aircheck rip_staging; do
  ensure_dir "$SRV_ROOT/$sub"
  log_info "$sub: created empty (regenerable -- see this script's header for how each is rebuilt)."
done

# ---- 3. music/ -- mountpoint only, NEVER populated -----------------------
# Deliberately NOT routed through ensure_dir()/guard_never_touch_music_library
# -- that guard exists to stop this script from ever WRITING CONTENT into
# the library path, and creating the empty mountpoint directory itself is
# not that; README.md's own step 4 does exactly this same
# `mkdir -p /srv/isadoraair/music` as ordinary, expected setup (so
# something has somewhere to mount onto). This code path structurally
# CANNOT populate the directory -- it is a bare mkdir/chown, nothing else
# -- so bypassing the generic guard here is safe and auditable, not a
# workaround of it.
if [ -n "$RESTORE_STAGING_ROOT" ]; then
  do_or_plan mkdir -p "$SRV_ROOT/music"
else
  do_or_plan sudo mkdir -p "$SRV_ROOT/music"
  do_or_plan sudo chown "$OWNER" "$SRV_ROOT/music"
fi
log_warn "music/: created as an EMPTY mountpoint only. The 717+ GB library itself is NOT restored by this tooling -- mount the real media disk at $SRV_ROOT/music separately (see docs/DISASTER_RECOVERY_RESTORE.md's 'Persistent storage mount' section) before considering the station ready to air. The database restore (stage 30) already brought back the full Track catalog; those rows will correctly point at files that don't exist here until the disk is attached."

# ---- 4. Reports (REPORTS_ROOT) -------------------------------------------
REPORTS_ROOT="/var/lib/isadoraair/reports"
ENV_FILE="$RESTORE_TARGET_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
  ENV_REPORTS_ROOT=$(grep -E '^REPORTS_ROOT=' "$ENV_FILE" | head -1 | cut -d= -f2- || true)
  [ -n "$ENV_REPORTS_ROOT" ] && REPORTS_ROOT="$ENV_REPORTS_ROOT"
fi
if [ -n "$RESTORE_STAGING_ROOT" ]; then
  REPORTS_ROOT="$RESTORE_STAGING_ROOT${REPORTS_ROOT}"
fi
log_info "Reports root: $REPORTS_ROOT"
if [ -n "$RESTORE_STAGING_ROOT" ]; then
  do_or_plan mkdir -p "$REPORTS_ROOT"
else
  do_or_plan sudo mkdir -p "$REPORTS_ROOT"
  do_or_plan sudo chown "$OWNER" "$REPORTS_ROOT"
fi
if grep -qE '^(\./)?reports/' <<< "$LISTING"; then
  if [ "$RESTORE_MODE" = "apply" ]; then
    log_apply "restoring reports/ -> $REPORTS_ROOT"
    tar -xzf "$RESTORE_ARCHIVE" -C "$REPORTS_ROOT" --strip-components=2 './reports'
    [ -z "$RESTORE_STAGING_ROOT" ] && sudo chown -R "$OWNER" "$REPORTS_ROOT"
    REPORT_FILE_COUNT=$(find "$REPORTS_ROOT" -type f | wc -l)
    log_info "reports: restored, $REPORT_FILE_COUNT file(s) -- durable royalty/SoundExchange filings, not treated as cache (see docs/DISASTER_RECOVERY.md's 'Reports' section for why)."
  else
    log_plan "tar -xzf <archive> -C $REPORTS_ROOT --strip-components=2 ./reports"
  fi
else
  log_warn "reports: archive has no reports/ entries -- may be legitimate (no filings generated yet)."
fi

# ---- 5. StereoTool .sts processing profile(s) -----------------------------
log_info "StereoTool profile directory: $STEREOTOOL_DIR"
if [ -n "$RESTORE_STAGING_ROOT" ]; then
  do_or_plan mkdir -p "$STEREOTOOL_DIR"
else
  do_or_plan sudo mkdir -p "$STEREOTOOL_DIR"
  do_or_plan sudo chown "$OWNER" "$STEREOTOOL_DIR"
fi
STS_COUNT_IN_ARCHIVE=$(grep -cE '^(\./)?stereotool/.*\.sts$' <<< "$LISTING" || true)
if [ "$STS_COUNT_IN_ARCHIVE" -gt 0 ]; then
  if [ "$RESTORE_MODE" = "apply" ]; then
    log_apply "restoring $STS_COUNT_IN_ARCHIVE .sts profile(s) -> $STEREOTOOL_DIR"
    tar -xzf "$RESTORE_ARCHIVE" -C "$STEREOTOOL_DIR" --strip-components=2 './stereotool'
    [ -z "$RESTORE_STAGING_ROOT" ] && sudo chown -R "$OWNER" "$STEREOTOOL_DIR"
    log_info "StereoTool profile(s): restored ($STS_COUNT_IN_ARCHIVE file(s))."
  else
    log_plan "tar -xzf <archive> -C $STEREOTOOL_DIR --strip-components=2 ./stereotool"
  fi
else
  log_warn "StereoTool: archive has no stereotool/*.sts entries -- nothing to restore (may be legitimate if StereoTool isn't used at all)."
fi

# ---- StereoTool manual-checkpoint checklist (Phase 4 spec section 16) ----
# The binary and license are NEVER part of this backup or this repo --
# proprietary, externally reprovisioned. This is a read-only informational
# checklist, not a gate that blocks this stage.
log_info "StereoTool readiness checklist (manual items are NOT automated by this tooling):"
if [ "$RESTORE_MODE" = "apply" ] && [ -n "$(ls -A "$STEREOTOOL_DIR" 2>/dev/null)" ]; then
  log_info "  [x] Profile (.sts) restored -- see $STEREOTOOL_DIR"
else
  log_info "  [ ] Profile (.sts) restored -- none found; see above"
fi
if [ -n "$STEREOTOOL_BIN" ] && [ -x "$STEREOTOOL_BIN" ]; then
  log_info "  [x] Binary installed and executable -- $STEREOTOOL_BIN"
else
  log_warn "  [ ] Binary installed -- NOT verified (pass --stereotool-bin PATH to check, or confirm manually). StereoTool's binary is proprietary and must be obtained/installed outside this tooling."
fi
log_warn "  [ ] License/config present -- cannot be checked automatically; confirm manually per the vendor's own instructions."
log_warn "  [ ] Service unit valid -- installed and syntax-checked by 90-system-config.sh, not this stage; run that stage next."
log_info "See docs/DISASTER_RECOVERY_RESTORE.md's 'StereoTool' section for the full manual handoff procedure."

# ---- 6. Readiness signal --------------------------------------------------
if [ "$RESTORE_MODE" = "apply" ]; then
  MUSIC_HAS_CONTENT=0
  if [ -d "$SRV_ROOT/music" ] && [ -n "$(ls -A "$SRV_ROOT/music" 2>/dev/null)" ]; then
    MUSIC_HAS_CONTENT=1
  fi
  if [ "$MUSIC_HAS_CONTENT" -eq 1 ]; then
    log_info "music/ is non-empty -- looks like the library disk is already attached/mounted here. Full station-content readiness."
  else
    log_warn "music/ is empty -- 'application can boot' does NOT mean 'station is ready to air'. See docs/DISASTER_RECOVERY.md's 'Music library' section: this is an expected, separate readiness gate, not a failure of this stage."
  fi
fi

log_info "40-station-content: $( [ "$RESTORE_MODE" = apply ] && echo PASS || echo "PLAN complete" )"
