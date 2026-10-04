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
#   - WEATHER_DATA_DIR (default /var/lib/isadoraair/weather) -- r0043:
#     also normalizes a known-legacy .env value pointing inside the
#     weather-ingest companion's own source-checkout namespace, before
#     Stage 60's first Django import can materialize it there and
#     poison Stage 80's companion clone. See section 5 below and
#     docs/DISASTER_RECOVERY_STATUS.md.
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

# ---- 0. REPORTS_ROOT path safety -- BEFORE this stage mutates anything ----
# P0 1.2: REPORTS_ROOT comes from the restored .env, so it is data, not
# trusted configuration. content_root_safety.py reads it with python-
# decouple's own semantics (last assignment wins -- exactly what Django
# will run with), canonicalizes it, and refuses /, system trees, shared
# anchors (/srv, /var/lib, /srv/isadoraair, $HOME, ...) and their
# ancestors, every application/tooling checkout, every other managed
# station root, and symlinks resolving into any of those. Under
# --staging-root the LIVE value is judged identically, so a staged
# rehearsal fails closed on the same .env a real restore would. Nothing
# in sections 1-6 runs unless this passes.
ENV_FILE="$RESTORE_TARGET_ROOT/.env"
CONTENT_ROOT_SAFETY="$SCRIPT_DIR/content_root_safety.py"
TOOLING_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPORTS_CHECK_ARGS=(check --key REPORTS_ROOT --env-file "$ENV_FILE"
  --target-root "$RESTORE_TARGET_ROOT" --tooling-root "$TOOLING_ROOT")
[ -n "$RESTORE_STAGING_ROOT" ] && REPORTS_CHECK_ARGS+=(--staging-root "$RESTORE_STAGING_ROOT")
if ! REPORTS_ROOT=$(python3 "$CONTENT_ROOT_SAFETY" "${REPORTS_CHECK_ARGS[@]}"); then
  log_error "Refusing: REPORTS_ROOT from $ENV_FILE failed restore path-safety validation (see above). This stage has created, changed and extracted nothing. Correct REPORTS_ROOT in $ENV_FILE to a dedicated reports directory and re-run."
  exit 1
fi
log_info "Reports root validated: $REPORTS_ROOT"

# WEATHER_DATA_DIR gets the same treatment (P0 1.2 follow-up -- a restored
# WEATHER_DATA_DIR=/etc previously led to `sudo chown $OWNER /etc` and
# `sudo chmod 0755 /etc`). Resolved here with decouple semantics, then the
# r0043 known-legacy recognition (section 5) decides which value will be
# used: a recognized legacy value is replaced by the canonical location,
# so that is the value judged. Either way the path is validated -- live
# value, in every mode -- before anything below runs.
CANONICAL_WEATHER_DATA_DIR="/var/lib/isadoraair/weather"
LEGACY_WEATHER_NAMESPACE="$(restore_default_companions_root)/weather-ingest"
WEATHER_DATA_DIR_VALUE=$(python3 "$CONTENT_ROOT_SAFETY" value --key WEATHER_DATA_DIR --env-file "$ENV_FILE")
WEATHER_IS_LEGACY=0
case "$WEATHER_DATA_DIR_VALUE" in
  "$LEGACY_WEATHER_NAMESPACE"|"$LEGACY_WEATHER_NAMESPACE"/*) WEATHER_IS_LEGACY=1 ;;
esac
WEATHER_CHECK_ARGS=(check --key WEATHER_DATA_DIR --env-file "$ENV_FILE"
  --target-root "$RESTORE_TARGET_ROOT" --tooling-root "$TOOLING_ROOT")
[ -n "$RESTORE_STAGING_ROOT" ] && WEATHER_CHECK_ARGS+=(--staging-root "$RESTORE_STAGING_ROOT")
[ "$WEATHER_IS_LEGACY" -eq 1 ] && WEATHER_CHECK_ARGS+=(--value "$CANONICAL_WEATHER_DATA_DIR")
if ! WEATHER_DATA_DIR=$(python3 "$CONTENT_ROOT_SAFETY" "${WEATHER_CHECK_ARGS[@]}"); then
  log_error "Refusing: WEATHER_DATA_DIR from $ENV_FILE failed restore path-safety validation (see above). This stage has created, changed and extracted nothing. Correct WEATHER_DATA_DIR in $ENV_FILE to a dedicated weather-data directory and re-run."
  exit 1
fi
log_info "Weather data directory validated: $WEATHER_DATA_DIR"

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

# ---- 4. Reports (REPORTS_ROOT, validated in section 0) --------------------
# Ownership is scoped to exactly what this stage restores: the reports
# directory itself (non-recursive, never through a symlink) and each
# archive member extracted into it. There is deliberately no `chown -R`:
# a pre-existing dedicated reports directory (e.g. on its own mounted
# filesystem) keeps every file this restore did not write untouched.
log_info "Reports root: $REPORTS_ROOT"
if [ -n "$RESTORE_STAGING_ROOT" ]; then
  do_or_plan mkdir -p -- "$REPORTS_ROOT"
else
  do_or_plan sudo mkdir -p -- "$REPORTS_ROOT"
  do_or_plan sudo chown -h -- "$OWNER" "$REPORTS_ROOT"
fi
if grep -qE '^(\./)?reports/' <<< "$LISTING"; then
  if [ "$RESTORE_MODE" = "apply" ]; then
    # Re-judge after establishing the directory (closes a swap between
    # section 0 and here), then validate every member before extraction.
    REVALIDATED_REPORTS_ROOT=$(python3 "$CONTENT_ROOT_SAFETY" "${REPORTS_CHECK_ARGS[@]}")
    if [ "$REVALIDATED_REPORTS_ROOT" != "$REPORTS_ROOT" ]; then
      log_error "Refusing: REPORTS_ROOT now resolves to $REVALIDATED_REPORTS_ROOT, not the validated $REPORTS_ROOT -- nothing extracted."
      exit 1
    fi
    REPORTS_MEMBERS_FILE="$(mktemp)"
    trap 'rm -f -- "$REPORTS_MEMBERS_FILE"' EXIT
    python3 "$CONTENT_ROOT_SAFETY" members --archive "$RESTORE_ARCHIVE" --prefix reports \
      --output "$REPORTS_MEMBERS_FILE" > /dev/null
    log_apply "restoring reports/ -> $REPORTS_ROOT"
    tar -xzf "$RESTORE_ARCHIVE" -C "$REPORTS_ROOT" --strip-components=2 './reports'
    if [ -z "$RESTORE_STAGING_ROOT" ]; then
      log_apply "sudo chown (no-follow, exact restored members only) $OWNER under $REPORTS_ROOT"
      sudo python3 -I "$CONTENT_ROOT_SAFETY" chown-members --root "$REPORTS_ROOT" --owner "$OWNER" \
        --members-file "$REPORTS_MEMBERS_FILE" > /dev/null
    fi
    rm -f -- "$REPORTS_MEMBERS_FILE"
    REPORT_FILE_COUNT=$(find "$REPORTS_ROOT" -type f | wc -l)
    log_info "reports: restored, $REPORT_FILE_COUNT file(s) -- durable royalty/SoundExchange filings, not treated as cache (see docs/DISASTER_RECOVERY.md's 'Reports' section for why)."
  else
    log_plan "validate archive reports/ members (plain relative regular files/directories only)"
    log_plan "tar -xzf <archive> -C $REPORTS_ROOT --strip-components=2 ./reports"
    if [ -z "$RESTORE_STAGING_ROOT" ]; then
      log_plan "sudo chown (no-follow, exact restored members only) $OWNER under $REPORTS_ROOT"
    fi
  fi
else
  log_warn "reports: archive has no reports/ entries -- may be legitimate (no filings generated yet)."
fi

# ---- 5. Weather data directory (WEATHER_DATA_DIR) ------------------------
# r0048 note: this section is unaffected by weather-ingest's in-tree
# import -- it recognizes and repairs an OLD *.env* value from a
# LEGACY-era backup (WEATHER_DATA_DIR, a runtime-DATA path, not the
# weather-ingest SOURCE checkout location), regardless of whether the
# revision being restored is modern or legacy. Retained unchanged so a
# genuinely historical archive (r0042-era or earlier) restores exactly
# as it always has -- see docs/DISASTER_RECOVERY_STATUS.md's incident
# record.
# r0043: a restored .env can carry a legacy WEATHER_DATA_DIR pointing
# INSIDE the weather-ingest companion's own source-checkout namespace
# (e.g. $HOME/weather-ingest/data) -- a real, confirmed E8 defect:
# weather/services.py's own module-level `DATA_DIR.mkdir(parents=True,
# exist_ok=True)`, triggered by Stage 60's very first Django import,
# then manufactures a non-empty, non-Git companion-checkout directory
# BEFORE Stage 80 ever runs -- so Stage 80's own (correct, must-stay-
# strict) non-Git-collision refusal fires on a directory IsadoraAir
# itself created. Recognized ONLY: a value that resolves inside
# <companions-default-root>/weather-ingest/ -- the exact, known legacy
# namespace this defect can leave behind (restore_default_companions_root,
# lib.sh -- the SAME default 80-companions.sh itself resolves
# COMPANIONS_ROOT from). Any OTHER value -- the current canonical
# default, or a genuine operator-chosen custom path outside that one
# namespace -- is NEVER rewritten; only this one recognized-legacy case
# is normalized, here, before Stage 60 can ever import Django.
#
# This is a deliberate, narrow exception to this stage's general
# "restore exactly what was backed up" posture -- 20-application.sh's
# own .env IS still byte-faithful at the moment IT finishes; this
# happens one stage later, specifically because the value is
# objectively wrong for ANY restore of this archive (a companion
# project's own checkout is not a legitimate runtime-data home in the
# current architecture), never merely an artifact of --staging-root.
# See docs/DISASTER_RECOVERY_STATUS.md for the full incident record.
# P0 1.2: the value, its legacy recognition and its path safety were all
# resolved in section 0 (CANONICAL_WEATHER_DATA_DIR, LEGACY_WEATHER_NAMESPACE,
# WEATHER_DATA_DIR_VALUE, WEATHER_IS_LEGACY, and the validated, effective
# WEATHER_DATA_DIR -- already staging-prefixed where applicable).
case "$WEATHER_IS_LEGACY" in
  1)
    log_warn "WEATHER_DATA_DIR=$WEATHER_DATA_DIR_VALUE is a known legacy value inside the weather-ingest companion's own source-checkout namespace ($LEGACY_WEATHER_NAMESPACE) -- normalizing $ENV_FILE to the canonical runtime-data location $CANONICAL_WEATHER_DATA_DIR before Stage 60's first Django import can materialize it (r0043 -- see docs/DISASTER_RECOVERY_STATUS.md)."
    if [ "$RESTORE_MODE" = "apply" ] && [ -f "$ENV_FILE" ]; then
      WEATHER_ENV_TMP="$(mktemp)"
      grep -vE '^WEATHER_DATA_DIR=' "$ENV_FILE" > "$WEATHER_ENV_TMP" || true
      printf 'WEATHER_DATA_DIR=%s\n' "$CANONICAL_WEATHER_DATA_DIR" >> "$WEATHER_ENV_TMP"
      install -m 0600 "$WEATHER_ENV_TMP" "$ENV_FILE"
      rm -f "$WEATHER_ENV_TMP"
      log_info "$ENV_FILE: WEATHER_DATA_DIR normalized to $CANONICAL_WEATHER_DATA_DIR (every other key left byte-for-byte unchanged)."
    else
      log_plan "normalize WEATHER_DATA_DIR in $ENV_FILE to $CANONICAL_WEATHER_DATA_DIR"
    fi
    ;;
  *)
    log_info "WEATHER_DATA_DIR=$WEATHER_DATA_DIR_VALUE is not a recognized legacy value -- left unchanged."
    ;;
esac
log_info "Weather data directory: $WEATHER_DATA_DIR"
guard_never_touch_music_library "$WEATHER_DATA_DIR"
# Explicit, deterministic mode -- a plain `mkdir -p` leaves a freshly-
# created directory at whatever 0777-minus-umask happens to produce (the
# exact class of bug r0041/r0042 already fixed for other restore-tooling
# destinations); asserted here independent of ambient umask, every run,
# whether the directory is fresh or pre-existing. P0 1.2: creation,
# ownership and mode go through content_root_safety.py's `establish`,
# which touches exactly this one validated directory (never recursively)
# through no-follow directory descriptors, so neither it nor any ancestor
# can be a symlink redirecting the change into another tree.
if [ -n "$RESTORE_STAGING_ROOT" ]; then
  do_or_plan python3 -I "$CONTENT_ROOT_SAFETY" establish --root "$WEATHER_DATA_DIR" --mode 0755
else
  do_or_plan sudo python3 -I "$CONTENT_ROOT_SAFETY" establish --root "$WEATHER_DATA_DIR" --owner "$OWNER" --mode 0755
fi

# ---- 6. StereoTool .sts processing profile(s) -----------------------------
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
# The binary is NEVER part of this backup or this repo -- proprietary,
# externally reprovisioned. This is a read-only informational checklist,
# not a gate that blocks this stage.
#
# 2026-08-18 disaster-recovery Phase 4.5 final follow-up: the license
# item below is deliberately NOT a log_warn -- confirmed operational
# behavior is that StereoTool runs and processes audio with no license
# entered at all (an occasional audio watermark every few hours is the
# only unlicensed penalty for the feature set in use), so its absence
# here is expected and non-blocking, not a gap to flag. See
# docs/DISASTER_RECOVERY_RESTORE.md's "StereoTool" section.
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
log_info "  [ ] License entered -- NOT a blocker; StereoTool runs unlicensed (occasional watermark) until entered manually post-restore, ~1 minute once the system is operational. Not automated or checked by this tooling."
log_warn "  [ ] Service unit valid -- installed and syntax-checked by 90-system-config.sh, not this stage; run that stage next."
log_info "See docs/DISASTER_RECOVERY_RESTORE.md's 'StereoTool' section for the full manual handoff procedure."

# ---- 7. Readiness signal --------------------------------------------------
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

if [ "$RESTORE_MODE" = "apply" ]; then
  restore_ledger_record "40-station-content"
fi
log_info "40-station-content: $( [ "$RESTORE_MODE" = apply ] && echo PASS || echo "PLAN complete" )"
