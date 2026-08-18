#!/usr/bin/env bash
# deploy/restore/10-packages.sh -- IsadoraAir 1.2 Phase 4.
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
while [ $# -gt 0 ]; do
  case "$1" in
    --with-cd-rip) WITH_CD_RIP=1; shift ;;
    --with-kokoro-tts) WITH_KOKORO_TTS=1; shift ;;
    --with-syndicated-selenium) WITH_SYNDICATED_SELENIUM=1; shift ;;
    --with-backup-encryption) WITH_BACKUP_ENCRYPTION=1; shift ;;
    --with-all-optional) WITH_CD_RIP=1; WITH_KOKORO_TTS=1; WITH_SYNDICATED_SELENIUM=1; WITH_BACKUP_ENCRYPTION=1; shift ;;
    --skip-heaac-build) SKIP_HEAAC_BUILD=1; shift ;;
    *) log_error "10-packages.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done

log_info "=== 10-packages ==="

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

ALREADY=()
MISSING=()
for pkg in "${RESOLVED[@]}"; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    ALREADY+=("$pkg")
  else
    MISSING+=("$pkg")
  fi
done

log_info "Already installed: ${#ALREADY[@]} package(s)"
log_info "Missing: ${#MISSING[@]} package(s)$( [ "${#MISSING[@]}" -gt 0 ] && printf ' -- %s' "${MISSING[*]}" )"

if [ "${#MISSING[@]}" -eq 0 ]; then
  log_info "10-packages: PASS (nothing to install)"
  exit 0
fi

if [ "$RESTORE_MODE" = "apply" ]; then
  log_apply "sudo apt-get update && sudo apt-get install -y ${MISSING[*]}"
  sudo apt-get update
  sudo apt-get install -y "${MISSING[@]}"
  log_info "10-packages: PASS (installed ${#MISSING[@]} package(s))"
else
  log_plan "apt-get install -y ${MISSING[*]}"
  log_info "10-packages: PLAN complete (${#MISSING[@]} package(s) would be installed)"
fi
