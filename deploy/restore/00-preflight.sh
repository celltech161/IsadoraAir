#!/usr/bin/env bash
# deploy/restore/00-preflight.sh -- IsadoraAir 1.2 Phase 4.
#
# First stage of the restore. Entirely read-only regardless of
# --plan/--apply (there is nothing to "apply" here -- preflight never
# writes anything itself, it only checks). Confirms:
#   1. This looks like a Debian/Ubuntu host (apt-based) -- the rest of
#      this tooling assumes that.
#   2. The OS release roughly matches the supported baseline
#      (Ubuntu 26.04 -- see docs/RUNTIME_BASELINE.md); a mismatch is a
#      WARNING, not a hard failure, since "close enough" Debian/Ubuntu
#      derivatives are plausible restore targets even if not the tested
#      one.
#   3. --archive was given and passes inspect_backup.sh's full
#      validation -- refuses to proceed with an invalid/incomplete
#      archive, per the Phase 4 safety boundary ("the restore tooling
#      should validate the archive before doing anything destructive").
#   4. Resolves and prints the effective mode/target-root/db-name that
#      every later stage will use, so an operator sees exactly what a
#      subsequent --apply run would target before running it.
#
# Usage:
#   deploy/restore/00-preflight.sh --archive /path/to/backup.tar.gz [--plan|--apply] [--staging-root PATH]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

restore_parse_common_args "$@"

log_info "=== 00-preflight ==="

# ---- 1/2. OS check -------------------------------------------------------
if [ ! -f /etc/os-release ]; then
  log_error "No /etc/os-release -- cannot determine OS. This tooling targets Debian/Ubuntu."
  exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
log_info "Detected OS: ${PRETTY_NAME:-unknown} (ID=${ID:-unknown} VERSION_ID=${VERSION_ID:-unknown})"

if [ "${ID:-}" != "ubuntu" ] && [ "${ID_LIKE:-}" != *"debian"* ]; then
  log_error "This host does not look like Ubuntu or a Debian derivative (ID=${ID:-unknown}). This tooling assumes apt/dpkg. Aborting."
  exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
  log_error "apt-get not found despite an apt-like /etc/os-release -- unusual environment, aborting."
  exit 1
fi

if [ "${VERSION_ID:-}" != "26.04" ]; then
  log_warn "VERSION_ID=${VERSION_ID:-unknown}, not the supported baseline (Ubuntu 26.04 -- see docs/RUNTIME_BASELINE.md). Proceeding, but package names/versions in deploy/packages-ubuntu-26.04.txt were only verified against 26.04."
else
  log_info "OS matches the supported baseline (Ubuntu 26.04)."
fi

# ---- 3. Backup archive validation ----------------------------------------
if [ -z "$RESTORE_ARCHIVE" ]; then
  log_error "No --archive given. A restore needs a validated backup archive to proceed from -- see deploy/restore/inspect_backup.sh."
  exit 1
fi
log_info "Validating backup archive: $RESTORE_ARCHIVE"
if ! "$SCRIPT_DIR/inspect_backup.sh" "$RESTORE_ARCHIVE"; then
  log_error "Backup archive failed validation (see output above). Refusing to proceed -- fix or choose a different archive."
  exit 1
fi
log_info "Backup archive validation: PASS"

# ---- 4. Production-target guard (checked here too, up front, so a bad
#         combination of flags is caught before any later stage starts
#         doing real work -- each later stage ALSO checks this itself,
#         since stages are independently runnable, but surfacing it here
#         first gives the clearest, earliest failure for the common case
#         of running restore.sh end-to-end). ---------------------------
guard_production_target

log_info "Preflight complete. Effective plan:"
log_info "  mode:        $RESTORE_MODE"
log_info "  target_root: $RESTORE_TARGET_ROOT"
log_info "  db_name:     $RESTORE_DB_NAME"
log_info "  archive:     $RESTORE_ARCHIVE"
if [ -n "$RESTORE_STAGING_ROOT" ]; then
  log_info "  staging_root: $RESTORE_STAGING_ROOT (isolated -- production is not touched)"
fi
log_info "00-preflight: PASS"
