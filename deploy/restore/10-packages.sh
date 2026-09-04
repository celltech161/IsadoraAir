#!/usr/bin/env bash
# deploy/restore/10-packages.sh -- IsadoraAir 1.2 Phase 4 / r0038 (E8).
#
# Bootstraps OS packages from the Phase 3 authoritative manifest
# (deploy/packages-ubuntu-26.04.txt), distinguishing required-generic
# from optional-station-integration from build-only, per the Phase 4
# safety boundary section 7 ("do not install every optional package
# blindly if the restore target does not need it").
#
# What installs by default (no flags): CORE + AUDIO_GSTREAMER +
# BUILD_HEAAC -- these three are needed by every IsadoraAir install that
# plays audio and streams at all; BUILD_HEAAC is included by default
# because 50-native-deps.sh always needs it and it's small
# (autoconf/automake/libtool/pkg-config), not because every station
# necessarily uses HE-AAC output.
#
# Optional station-integration groups, opt-in only:
#   --with-cd-rip              OPTIONAL_CD_RIP (whipper, cdparanoia, flac, libdiscid0)
#   --with-kokoro-tts          OPTIONAL_KOKORO_TTS (espeak-ng)
#   --with-syndicated-selenium OPTIONAL_SYNDICATED_SELENIUM (chromium-browser, chromium-chromedriver)
#   --with-backup-encryption   OPTIONAL_BACKUP_ENCRYPTION (age) -- only needed
#                              if BACKUP_RECOVERY_AGE_RECIPIENT(_FILE) will be
#                              configured on this install; see
#                              deploy/encrypt_recovery_credentials.sh
#   --with-all-optional        all four of the above
#
# --skip-heaac-build            omit BUILD_HEAAC even from the default set
#
# In --plan mode, never touches apt or needs root -- checks each
# package's install state via `dpkg -s` (unprivileged) and reports
# already-installed vs. would-install. In --apply mode, runs
# `sudo apt-get update` once then `sudo apt-get install -y` with the
# resolved package list.
#
# ---------------------------------------------------------------------
# r0038 (E8 offline restore hardening) -- two new, independent, opt-in
# flags for a genuinely offline clean-machine restore. Both default to
# unset, which preserves the exact behavior above for an ordinary
# connected/non-E8 install:
#
#   --apt-repo-dir PATH   When given, every apt-get invocation in this
#                          stage (update / install / download) is scoped
#                          EXCLUSIVELY to a local `deb [trusted=yes]
#                          file://PATH ./` source via `-o Dir::Etc::
#                          sourcelist=...  -o Dir::Etc::sourceparts=
#                          /dev/null` -- apt never even attempts a
#                          configured online mirror, so a broken/
#                          incomplete local closure fails closed (apt's
#                          own dependency error) instead of silently
#                          falling back to the network. PATH is an
#                          operator-supplied path to a closure built by
#                          build_offline_closure.py's apt-closure step
#                          (Packages/Packages.gz + .deb files) -- never
#                          hard-coded here.
#
#   --snap-dir PATH        Only meaningful combined with
#                          --with-syndicated-selenium (or the flag that
#                          implies it, --with-all-optional -- see above).
#                          Selects "local-snap mode":
#                          chromium-browser/chromium-chromedriver
#                          are pulled OUT of the ordinary apt
#                          install list and handled by a dedicated,
#                          verified, offline sequence instead (see
#                          "Local-snap mode" below). PATH must contain a
#                          complete snap closure + snap-manifest.json, as
#                          produced by build_offline_closure.py's
#                          snap-closure step -- verified via
#                          offline_snap_install.py, which fails closed
#                          (nonzero exit, no partial output) on any
#                          missing file, SHA256 mismatch, missing
#                          snapd/chromium, or invalid install order. This
#                          verification runs in BOTH --plan and --apply
#                          (read-only, no root needed) so a broken
#                          closure is caught before any real machine is
#                          touched, not discovered mid-restore.
#
# ## Local-snap mode: why, and the exact sequence
#
# Ubuntu 26.04's `chromium-browser` apt package is a thin Snap Store
# TRANSITION package, not the browser itself -- its preinst checks for
# /run/snapd.socket and, if present, shells out to `snap info chromium` /
# `snap install chromium`. With E8's network deliberately blocked, that
# hangs/times out and the package's own script falls into an interactive
# debconf Retry/Abort/Skip prompt -- unacceptable for an unattended
# restore, and proven (E8 acceptance run) to happen even when a working
# Chromium snap is ALREADY installed locally: the preinst does not check
# "is chromium already usable", it always tries the store first if
# /run/snapd.socket exists at all. The genuinely offline fix, proven by
# hand during that same run and reproduced here as unattended tooling:
#
#   1. Install the FULL preserved snap closure locally first (snapd
#      system snap + every base/content/runtime snap + chromium itself),
#      ack'ing each assertion normally via `snap ack` -- never
#      `--dangerous`/`--devmode`. Chromium is installed here, BEFORE the
#      Ubuntu wrapper packages (requirement: the real browser runtime
#      must already exist locally before the transition package's
#      preinst can possibly run at all).
#   2. Stop snapd.socket and snapd.service, and verify (systemctl
#      is-active) that both are actually inactive -- not merely that the
#      stop command returned 0.
#   3. Stopping snapd leaves a STALE /run/snapd.socket filesystem inode
#      behind (systemd removes the unit's own tracking, not necessarily
#      the socket file snapd itself created). Before removing it, verify
#      via `ss -lxH src /run/snapd.socket` that nothing is still
#      listening on it -- if anything is, this refuses to remove it and
#      aborts rather than risk deleting a live socket out from under an
#      active snapd. Only a confirmed-stale, listener-free socket file is
#      ever removed.
#   4. With /run/snapd.socket absent, chromium-browser's preinst takes
#      its OWN documented "system doesn't have a working snapd, skipping"
#      branch and never attempts Snap Store access at all -- this is the
#      actual fix, not merely a debconf frontend setting.
#      DEBIAN_FRONTEND=noninteractive is still set as defense in depth.
#      `dpkg -i` is used directly (not `apt-get install`) for exactly
#      these two files, matching the sequence proven during the E8
#      acceptance run -- both packages' own dependencies (debconf; each
#      other) are already satisfied by this point.
#   5. Restart snapd.socket/snapd.service (if they were active
#      beforehand) and verify they came back up -- always attempted, via
#      a trap, even if an earlier step in this sequence fails, so a
#      failed restore attempt does not leave the host's snapd
#      permanently stopped.
#   6. Validate chromium-browser --version and chromedriver --version,
#      confirm their major versions match (they wrap the same underlying
#      Chromium snap), and run a bounded (timeout-guarded) headless
#      Chromium smoke test -- logged clearly either way, but treated as
#      non-fatal (AppArmor/D-Bus sandbox warnings are expected and did
#      not prevent execution during the E8 acceptance run).
#
# Local-snap mode NEVER calls `snap install chromium` (network form,
# no local path) or ordinary `apt-get install chromium-browser
# chromium-chromedriver` -- every snap install uses a local .snap file
# path, and the two wrapper packages are always installed via `dpkg -i`
# against locally-resolved .deb files. There is no fallback path.
#
# See docs/DISASTER_RECOVERY_RESTORE.md's "Offline package/snap closure"
# section for the operator-facing walkthrough and
# deploy/restore/offline_snap_install.py / build_offline_closure.py for
# the manifest verification and closure-building tools this depends on.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PACKAGES_FILE="$REPO_ROOT/deploy/packages-ubuntu-26.04.txt"

restore_parse_common_args "$@"
set -- "${RESTORE_REMAINING_ARGS[@]}"

WITH_CD_RIP=0
WITH_KOKORO_TTS=0
WITH_SYNDICATED_SELENIUM=0
WITH_BACKUP_ENCRYPTION=0
SKIP_HEAAC_BUILD=0
APT_REPO_DIR=""
SNAP_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --with-cd-rip) WITH_CD_RIP=1; shift ;;
    --with-kokoro-tts) WITH_KOKORO_TTS=1; shift ;;
    --with-syndicated-selenium) WITH_SYNDICATED_SELENIUM=1; shift ;;
    --with-backup-encryption) WITH_BACKUP_ENCRYPTION=1; shift ;;
    --with-all-optional) WITH_CD_RIP=1; WITH_KOKORO_TTS=1; WITH_SYNDICATED_SELENIUM=1; WITH_BACKUP_ENCRYPTION=1; shift ;;
    --skip-heaac-build) SKIP_HEAAC_BUILD=1; shift ;;
    --apt-repo-dir) APT_REPO_DIR="${2:?--apt-repo-dir needs a path}"; shift 2 ;;
    --apt-repo-dir=*) APT_REPO_DIR="${1#*=}"; shift ;;
    --snap-dir) SNAP_DIR="${2:?--snap-dir needs a path}"; shift 2 ;;
    --snap-dir=*) SNAP_DIR="${1#*=}"; shift ;;
    *) log_error "10-packages.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done

log_info "=== 10-packages ==="

# ---------------------------------------------------------------------
# r0038 code review: --apt-repo-dir + --with-syndicated-selenium with NO
# --snap-dir must fail closed, not silently fall through to the ordinary
# apt chromium-browser path. An operator who explicitly scoped apt to a
# strictly offline local repo has clearly signaled an offline/E8-style
# run -- letting Selenium/Chromium through the ordinary path in that
# situation would still hit the Snap Store transition-package hang this
# whole file exists to avoid (see the "Local-snap mode" section above),
# just later and more confusingly. This check runs BEFORE any apt/snap/
# dpkg action, in BOTH --plan and --apply -- a configuration error, not
# a runtime one, so there is nothing to preview differently between the
# two modes. Selenium + neither offline flag (ordinary connected
# install) and Selenium + --snap-dir (with or without --apt-repo-dir)
# both remain entirely unaffected by this check.
# ---------------------------------------------------------------------
if [ -n "$APT_REPO_DIR" ] && [ "$WITH_SYNDICATED_SELENIUM" -eq 1 ] && [ -z "$SNAP_DIR" ]; then
  log_error "10-packages.sh: --apt-repo-dir with --with-syndicated-selenium also requires --snap-dir -- on Ubuntu 26.04, chromium-browser is a Snap Store transition package, and the ordinary apt install path can re-enter its offline hang under a strictly offline apt repo. Provide --snap-dir (see docs/DISASTER_RECOVERY_RESTORE.md's 'Offline package/snap closure' section), or drop --apt-repo-dir for an ordinary connected install."
  exit 2
fi

# Overridable only for test isolation (see isadoraair/tests/
# test_restore_tooling.py's Stage10 local-snap functional tests) -- a
# real restore always uses the real /run/snapd.socket; this lets a test
# point the transition sequence's socket check at a throwaway path
# instead of the live host's actual snapd socket, without touching real
# system state to exercise the logic.
SNAPD_SOCKET_PATH="${RESTORE_SNAPD_SOCKET_PATH:-/run/snapd.socket}"

if [ ! -f "$PACKAGES_FILE" ]; then
  log_error "Package manifest not found: $PACKAGES_FILE"
  exit 1
fi
# shellcheck source=../packages-ubuntu-26.04.txt
source "$PACKAGES_FILE"

RESOLVED=("${CORE[@]}" "${AUDIO_GSTREAMER[@]}")
RESOLVED_LABEL="CORE + AUDIO_GSTREAMER"
if [ "$SKIP_HEAAC_BUILD" -ne 1 ]; then
  RESOLVED+=("${BUILD_HEAAC[@]}")
  RESOLVED_LABEL="$RESOLVED_LABEL + BUILD_HEAAC"
fi
if [ "$WITH_CD_RIP" -eq 1 ]; then
  RESOLVED+=("${OPTIONAL_CD_RIP[@]}")
  RESOLVED_LABEL="$RESOLVED_LABEL + OPTIONAL_CD_RIP"
fi
if [ "$WITH_KOKORO_TTS" -eq 1 ]; then
  RESOLVED+=("${OPTIONAL_KOKORO_TTS[@]}")
  RESOLVED_LABEL="$RESOLVED_LABEL + OPTIONAL_KOKORO_TTS"
fi
if [ "$WITH_SYNDICATED_SELENIUM" -eq 1 ]; then
  RESOLVED+=("${OPTIONAL_SYNDICATED_SELENIUM[@]}")
  RESOLVED_LABEL="$RESOLVED_LABEL + OPTIONAL_SYNDICATED_SELENIUM"
fi
if [ "$WITH_BACKUP_ENCRYPTION" -eq 1 ]; then
  RESOLVED+=("${OPTIONAL_BACKUP_ENCRYPTION[@]}")
  RESOLVED_LABEL="$RESOLVED_LABEL + OPTIONAL_BACKUP_ENCRYPTION"
fi

log_info "Package groups selected: $RESOLVED_LABEL"
log_info "Skipped (opt-in, not requested): $( [ "$WITH_CD_RIP" -eq 0 ] && echo -n 'OPTIONAL_CD_RIP ' )$( [ "$WITH_KOKORO_TTS" -eq 0 ] && echo -n 'OPTIONAL_KOKORO_TTS ' )$( [ "$WITH_SYNDICATED_SELENIUM" -eq 0 ] && echo -n 'OPTIONAL_SYNDICATED_SELENIUM ' )$( [ "$WITH_BACKUP_ENCRYPTION" -eq 0 ] && echo -n 'OPTIONAL_BACKUP_ENCRYPTION' )"

# ---------------------------------------------------------------------
# Local-snap mode resolution -- only when Selenium is selected AND
# --snap-dir was given. Verification (offline_snap_install.py) runs now,
# unprivileged, in BOTH --plan and --apply: an incomplete/corrupt closure
# must be caught here, not discovered mid-restore. The two Chromium
# wrapper packages are pulled out of the ordinary apt-managed list below
# either way -- LOCAL_SNAP_MODE picks which mechanism installs them.
# ---------------------------------------------------------------------
LOCAL_SNAP_MODE=0
if [ "$WITH_SYNDICATED_SELENIUM" -eq 1 ] && [ -n "$SNAP_DIR" ]; then
  LOCAL_SNAP_MODE=1
  require_cmd python3
  if [ ! -d "$SNAP_DIR" ]; then
    log_error "10-packages.sh: --snap-dir does not exist: $SNAP_DIR"
    exit 1
  fi
  log_info "Local-snap mode: verifying offline snap closure at $SNAP_DIR ..."
  SNAP_PLAN_STATUS=0
  SNAP_PLAN_OUTPUT="$(python3 "$SCRIPT_DIR/offline_snap_install.py" plan --snap-dir "$SNAP_DIR" 2>&1)" || SNAP_PLAN_STATUS=$?
  if [ "$SNAP_PLAN_STATUS" -ne 0 ]; then
    log_error "10-packages.sh: offline snap closure verification FAILED (exit $SNAP_PLAN_STATUS) -- refusing to proceed. No fallback to the Snap Store."
    log_error "$SNAP_PLAN_OUTPUT"
    exit 1
  fi
  SNAP_PLAN_NAMES=()
  SNAP_PLAN_REVISIONS=()
  SNAP_PLAN_ASSERTS=()
  SNAP_PLAN_SNAPS=()
  while IFS=$'\t' read -r kind name revision assert_path snap_path; do
    [ "$kind" = "SNAP" ] || continue
    SNAP_PLAN_NAMES+=("$name")
    SNAP_PLAN_REVISIONS+=("$revision")
    SNAP_PLAN_ASSERTS+=("$assert_path")
    SNAP_PLAN_SNAPS+=("$snap_path")
  done <<< "$SNAP_PLAN_OUTPUT"
  log_info "Local-snap mode: closure verified -- ${#SNAP_PLAN_NAMES[@]} snap(s), install order: ${SNAP_PLAN_NAMES[*]}"
fi

# ---------------------------------------------------------------------
# Offline apt source override -- optional, independent of local-snap
# mode. When given, apt-get NEVER consults any configured online source
# for this stage's own update/install/download calls -- fail closed
# (apt's own unsatisfiable-dependency error) rather than a silent
# network fallback.
# ---------------------------------------------------------------------
APT_OPTS=()
if [ -n "$APT_REPO_DIR" ]; then
  if [ ! -d "$APT_REPO_DIR" ]; then
    log_error "10-packages.sh: --apt-repo-dir does not exist: $APT_REPO_DIR"
    exit 1
  fi
  APT_SOURCES_OVERRIDE="$(mktemp)"
  printf 'deb [trusted=yes] file://%s ./\n' "$APT_REPO_DIR" > "$APT_SOURCES_OVERRIDE"
  APT_OPTS=(-o "Dir::Etc::sourcelist=$APT_SOURCES_OVERRIDE" -o "Dir::Etc::sourceparts=/dev/null")
  log_info "Offline apt mode: apt-get scoped exclusively to $APT_REPO_DIR (no configured online sources)."
fi

# ---------------------------------------------------------------------
# Ordinary apt-managed packages (everything except, in local-snap mode,
# the two Chromium wrapper packages -- those are handled below instead).
# ---------------------------------------------------------------------
APT_RESOLVED=()
for pkg in "${RESOLVED[@]}"; do
  if [ "$LOCAL_SNAP_MODE" -eq 1 ] && { [ "$pkg" = "chromium-browser" ] || [ "$pkg" = "chromium-chromedriver" ]; }; then
    continue
  fi
  APT_RESOLVED+=("$pkg")
done

ALREADY=()
MISSING=()
for pkg in "${APT_RESOLVED[@]}"; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    ALREADY+=("$pkg")
  else
    MISSING+=("$pkg")
  fi
done

log_info "Already installed: ${#ALREADY[@]} package(s)"
log_info "Missing: ${#MISSING[@]} package(s)$( [ "${#MISSING[@]}" -gt 0 ] && printf ' -- %s' "${MISSING[*]}" )"

if [ "${#MISSING[@]}" -eq 0 ]; then
  log_info "10-packages: nothing to install via apt."
elif [ "$RESTORE_MODE" = "apply" ]; then
  log_apply "sudo apt-get update && sudo apt-get install -y ${MISSING[*]}"
  sudo apt-get "${APT_OPTS[@]}" update
  sudo apt-get "${APT_OPTS[@]}" install -y "${MISSING[@]}"
  log_info "10-packages: installed ${#MISSING[@]} apt package(s)."
else
  log_plan "apt-get install -y ${MISSING[*]}"
  log_info "10-packages: PLAN -- ${#MISSING[@]} apt package(s) would be installed."
fi

# ---------------------------------------------------------------------
# Local-snap mode: snap closure install, then the Chromium
# transition-package sequence. See this file's own header for the full
# rationale and exact sequence. Skipped entirely unless LOCAL_SNAP_MODE.
# ---------------------------------------------------------------------
if [ "$LOCAL_SNAP_MODE" -eq 1 ]; then
  log_info "--- Local-snap mode: Selenium/Chromium closure ---"

  for i in "${!SNAP_PLAN_NAMES[@]}"; do
    snap_name="${SNAP_PLAN_NAMES[$i]}"
    snap_revision="${SNAP_PLAN_REVISIONS[$i]}"
    snap_assert="${SNAP_PLAN_ASSERTS[$i]}"
    snap_file="${SNAP_PLAN_SNAPS[$i]}"
    installed_rev=""
    if command -v snap >/dev/null 2>&1; then
      installed_rev="$(snap list "$snap_name" 2>/dev/null | awk -v n="$snap_name" '$1==n {print $3}')" || true
    fi
    if [ "$installed_rev" = "$snap_revision" ]; then
      log_info "snap '$snap_name' already installed at revision $snap_revision -- skipping."
      continue
    fi
    if [ -n "$installed_rev" ]; then
      log_error "10-packages.sh: snap '$snap_name' is installed at revision $installed_rev but the closure manifest expects revision $snap_revision -- refusing to silently reinstall over a mismatched revision."
      exit 1
    fi
    do_or_plan sudo snap ack "$snap_assert"
    do_or_plan sudo snap install "$snap_file"
  done

  # ---- Chromium wrapper packages: resolve local .deb files -----------
  CHROMIUM_DL_DIR=""
  if [ "$RESTORE_MODE" = "apply" ]; then
    CHROMIUM_DL_DIR="$(mktemp -d)"
    log_apply "apt-get download chromium-browser chromium-chromedriver -> $CHROMIUM_DL_DIR"
    ( cd "$CHROMIUM_DL_DIR" && apt-get "${APT_OPTS[@]}" download chromium-browser chromium-chromedriver )
    CHROMIUM_BROWSER_DEB="$(ls "$CHROMIUM_DL_DIR"/chromium-browser_*.deb 2>/dev/null | head -n1)"
    CHROMIUM_CHROMEDRIVER_DEB="$(ls "$CHROMIUM_DL_DIR"/chromium-chromedriver_*.deb 2>/dev/null | head -n1)"
    if [ -z "$CHROMIUM_BROWSER_DEB" ] || [ -z "$CHROMIUM_CHROMEDRIVER_DEB" ]; then
      log_error "10-packages.sh: apt-get download did not produce both chromium-browser and chromium-chromedriver .deb files."
      exit 1
    fi

    require_cmd systemctl
    require_cmd dpkg
    require_cmd ss

    SNAPD_SOCKET_WAS_ACTIVE=0
    SNAPD_SERVICE_WAS_ACTIVE=0
    systemctl is-active --quiet snapd.socket && SNAPD_SOCKET_WAS_ACTIVE=1 || true
    systemctl is-active --quiet snapd.service && SNAPD_SERVICE_WAS_ACTIVE=1 || true
    SNAPD_RESTORE_DONE=0

    restore_snapd_state() {
      if [ "$SNAPD_RESTORE_DONE" -eq 1 ]; then
        return 0
      fi
      SNAPD_RESTORE_DONE=1
      if [ "$SNAPD_SOCKET_WAS_ACTIVE" -eq 1 ] || [ "$SNAPD_SERVICE_WAS_ACTIVE" -eq 1 ]; then
        log_apply "sudo systemctl start snapd.socket snapd.service (restoring pre-transition state)"
        sudo systemctl start snapd.socket snapd.service || log_warn "10-packages.sh: failed to restart snapd after the chromium transition sequence -- restart it manually: sudo systemctl start snapd.socket snapd.service"
      fi
    }
    trap restore_snapd_state EXIT

    log_apply "sudo systemctl stop snapd.socket snapd.service"
    sudo systemctl stop snapd.socket snapd.service

    if systemctl is-active --quiet snapd.socket || systemctl is-active --quiet snapd.service; then
      log_error "10-packages.sh: snapd.socket/snapd.service still active after 'systemctl stop' -- refusing to touch $SNAPD_SOCKET_PATH."
      exit 1
    fi
    log_info "Verified snapd.socket and snapd.service are inactive."

    if [ -e "$SNAPD_SOCKET_PATH" ]; then
      if ss -lxH src "$SNAPD_SOCKET_PATH" 2>/dev/null | grep -q .; then
        log_error "10-packages.sh: $SNAPD_SOCKET_PATH still has an active listener despite snapd being stopped -- refusing to remove it."
        exit 1
      fi
      log_apply "sudo rm -f $SNAPD_SOCKET_PATH (stale inode, no active listener confirmed)"
      sudo rm -f "$SNAPD_SOCKET_PATH"
    else
      log_info "$SNAPD_SOCKET_PATH already absent -- nothing to remove."
    fi

    log_apply "sudo DEBIAN_FRONTEND=noninteractive dpkg -i $CHROMIUM_BROWSER_DEB $CHROMIUM_CHROMEDRIVER_DEB"
    sudo DEBIAN_FRONTEND=noninteractive dpkg -i "$CHROMIUM_BROWSER_DEB" "$CHROMIUM_CHROMEDRIVER_DEB"

    restore_snapd_state
    trap - EXIT

    if [ "$SNAPD_SOCKET_WAS_ACTIVE" -eq 1 ] || [ "$SNAPD_SERVICE_WAS_ACTIVE" -eq 1 ]; then
      if systemctl is-active --quiet snapd.socket && systemctl is-active --quiet snapd.service; then
        log_info "snapd.socket and snapd.service restored to active."
      else
        log_error "10-packages.sh: snapd did not come back up after the chromium transition sequence."
        exit 1
      fi
    fi

    # ---- Validation --------------------------------------------------
    if command -v chromium-browser >/dev/null 2>&1 && command -v chromedriver >/dev/null 2>&1; then
      CHROMIUM_VERSION_OUT="$(chromium-browser --version 2>&1 || true)"
      CHROMEDRIVER_VERSION_OUT="$(chromedriver --version 2>&1 || true)"
      log_info "chromium-browser --version: $CHROMIUM_VERSION_OUT"
      log_info "chromedriver --version: $CHROMEDRIVER_VERSION_OUT"
      # Targeted at the "Chromium NNN..."/"ChromeDriver NNN..." version
      # line specifically -- NOT the first digit sequence anywhere in the
      # combined stdout+stderr blob, which can spuriously match an
      # unrelated warning line (e.g. "xdg-settings: not found" printed
      # earlier by the same command).
      CHROMIUM_MAJOR="$(printf '%s\n' "$CHROMIUM_VERSION_OUT" | grep -oE 'Chromium [0-9]+' | grep -oE '[0-9]+' | head -n1 || true)"
      CHROMEDRIVER_MAJOR="$(printf '%s\n' "$CHROMEDRIVER_VERSION_OUT" | grep -oE 'ChromeDriver [0-9]+' | grep -oE '[0-9]+' | head -n1 || true)"
      if [ -n "$CHROMIUM_MAJOR" ] && [ "$CHROMIUM_MAJOR" = "$CHROMEDRIVER_MAJOR" ]; then
        log_info "chromium-browser/chromedriver major version match: $CHROMIUM_MAJOR"
      else
        log_error "10-packages.sh: chromium-browser (major $CHROMIUM_MAJOR) and chromedriver (major $CHROMEDRIVER_MAJOR) do not match."
        exit 1
      fi

      SMOKE_MARKER="E8-CHROMIUM-OFFLINE-PASS"
      SMOKE_OUT="$(timeout 20s chromium-browser --headless --no-sandbox --disable-gpu --dump-dom "data:text/html,<html><body>${SMOKE_MARKER}</body></html>" 2>&1)" || true
      if printf '%s' "$SMOKE_OUT" | grep -q "$SMOKE_MARKER"; then
        log_info "Headless Chromium smoke test: PASS"
      else
        log_warn "Headless Chromium smoke test did not confirm the expected marker (non-fatal -- see deploy/restore/10-packages.sh header). Output: $SMOKE_OUT"
      fi
    else
      log_error "10-packages.sh: chromium-browser/chromedriver not found on PATH after installation."
      exit 1
    fi
  else
    log_plan "apt-get download chromium-browser chromium-chromedriver (into a temp dir)"
    log_plan "sudo systemctl stop snapd.socket snapd.service"
    log_plan "verify snapd.socket/snapd.service inactive; verify no listener on /run/snapd.socket before removing it"
    log_plan "sudo DEBIAN_FRONTEND=noninteractive dpkg -i <chromium-browser>.deb <chromium-chromedriver>.deb"
    log_plan "sudo systemctl start snapd.socket snapd.service (restore prior state)"
    log_plan "validate chromium-browser --version / chromedriver --version match; bounded headless smoke test"
  fi
  rm -rf "$CHROMIUM_DL_DIR" 2>/dev/null || true
fi

log_info "10-packages: PASS"
