#!/usr/bin/env bash
# deploy/restore/20-application.sh -- IsadoraAir 1.2 Phase 4.
#
# Reconstructs the application checkout at the target root. Implements
# the Git-vs-app.tar.gz model documented in full in
# deploy/restore/README.md's "Application-source recovery model"
# section -- short version:
#   - Git is the source of CODE: clone fresh, verify the MANIFEST.txt
#     SHA is reachable, checkout exactly that SHA (detached HEAD).
#   - app.tar.gz is the source of .env and media/ ONLY -- never code,
#     never .git, never venv/staticfiles/caches. Extracting code from
#     the tarball would silently drift the tree away from the Git SHA
#     the backup's database dump was actually taken alongside.
#
# Usage:
#   deploy/restore/20-application.sh --archive PATH [--plan|--apply]
#     [--staging-root PATH] [--repo-url URL] [--force-env]
#
# --repo-url defaults to git@github.com:celltech161/IsadoraAir.git --
# override for a fork or a differently-named remote.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

restore_parse_common_args "$@"
set -- "${RESTORE_REMAINING_ARGS[@]}"

REPO_URL="git@github.com:celltech161/IsadoraAir.git"
while [ $# -gt 0 ]; do
  case "$1" in
    --repo-url) REPO_URL="${2:?--repo-url needs a value}"; shift 2 ;;
    --repo-url=*) REPO_URL="${1#*=}"; shift ;;
    *) log_error "20-application.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done

log_info "=== 20-application ==="
guard_production_target
require_cmd git
require_cmd tar

if [ -z "$RESTORE_ARCHIVE" ] || [ ! -f "$RESTORE_ARCHIVE" ]; then
  log_error "No valid --archive given (got: '${RESTORE_ARCHIVE:-<none>}'). Run 00-preflight.sh first."
  exit 1
fi

# ---- Parse the Git SHA out of MANIFEST.txt -------------------------------
MANIFEST_CONTENT=$(tar -xzO -f "$RESTORE_ARCHIVE" ./MANIFEST.txt 2>/dev/null || tar -xzO -f "$RESTORE_ARCHIVE" MANIFEST.txt 2>/dev/null || true)
if [ -z "$MANIFEST_CONTENT" ]; then
  log_error "Could not extract MANIFEST.txt from $RESTORE_ARCHIVE"
  exit 1
fi
GIT_SHA=$(grep -E '^IsadoraAir Git SHA:' <<< "$MANIFEST_CONTENT" | sed -E 's/^IsadoraAir Git SHA:\s*//' || true)
if [ -z "$GIT_SHA" ] || [ "$GIT_SHA" = "unknown" ]; then
  log_error "MANIFEST.txt does not record a usable Git SHA (got: '${GIT_SHA:-<empty>}'). Cannot verify which commit this backup's database dump corresponds to -- refusing to guess (e.g. by defaulting to 'main'). See docs/DISASTER_RECOVERY_RESTORE.md for manual recovery guidance in this case."
  exit 1
fi
log_info "Backup recorded Git SHA: $GIT_SHA"
log_warn "MANIFEST.txt does not currently record whether the source tree was clean (uncommitted changes) at backup time -- a known, documented limitation (see docs/DISASTER_RECOVERY_RESTORE.md). This restore checks out exactly $GIT_SHA; any uncommitted changes present when the backup was taken are NOT recoverable from this archive."

# ---- Clone or verify existing checkout -----------------------------------
if [ -d "$RESTORE_TARGET_ROOT/.git" ]; then
  log_info "$RESTORE_TARGET_ROOT already has a .git directory -- will fetch + verify rather than re-clone."
  do_or_plan git -C "$RESTORE_TARGET_ROOT" fetch --all --tags
elif [ -e "$RESTORE_TARGET_ROOT" ] && [ -n "$(ls -A "$RESTORE_TARGET_ROOT" 2>/dev/null)" ]; then
  log_error "$RESTORE_TARGET_ROOT already exists, is non-empty, and is not a Git checkout (no .git/). Refusing to clone into it -- remove it first or choose a different --target-root/--staging-root if this is intentional."
  exit 1
else
  log_info "Cloning $REPO_URL into $RESTORE_TARGET_ROOT"
  do_or_plan mkdir -p "$(dirname "$RESTORE_TARGET_ROOT")"
  do_or_plan git clone "$REPO_URL" "$RESTORE_TARGET_ROOT"
fi

if [ "$RESTORE_MODE" = "apply" ]; then
  # ---- Verify the recorded SHA is actually reachable before touching
  #      anything else -- fail clearly rather than falling back to a
  #      branch tip that may not match the restored database. ---------
  if ! git -C "$RESTORE_TARGET_ROOT" cat-file -e "${GIT_SHA}^{commit}" 2>/dev/null; then
    log_error "Commit $GIT_SHA (recorded in the backup's MANIFEST.txt) is not reachable in the cloned repository. This can happen if history was rewritten (force-push) or the manifest is corrupt. Refusing to fall back to a branch tip -- investigate before proceeding. See docs/DISASTER_RECOVERY_RESTORE.md."
    exit 1
  fi
  log_apply "git -C $RESTORE_TARGET_ROOT checkout --detach $GIT_SHA"
  git -C "$RESTORE_TARGET_ROOT" checkout --detach "$GIT_SHA"
  log_info "Checked out $GIT_SHA (detached HEAD) -- matches the backup's database dump."
else
  log_plan "git -C $RESTORE_TARGET_ROOT checkout --detach $GIT_SHA (after verifying it's reachable)"
fi

# ---- Determine app.tar.gz's top-level directory name ---------------------
APP_LISTING=$(tar -xzO -f "$RESTORE_ARCHIVE" ./app.tar.gz 2>/dev/null | tar -tz 2>/dev/null || true)
if [ -z "$APP_LISTING" ]; then
  log_error "Could not list app.tar.gz contents from $RESTORE_ARCHIVE"
  exit 1
fi
APP_TOPDIR=$(grep -oE '^[^/]+' <<< "$APP_LISTING" | sort -u | head -1)
if [ -z "$APP_TOPDIR" ]; then
  log_error "Could not determine app.tar.gz's top-level directory name."
  exit 1
fi
log_info "app.tar.gz top-level directory: $APP_TOPDIR"

# ---- .env: extract by EXACT name only, never a glob that could also
#      catch .env.bak/.env.lock (see backup script's own exclude flags
#      -- belt and suspenders, since inspect_backup.sh already warns if
#      they somehow made it into an archive anyway). --------------------
ENV_TARGET="$RESTORE_TARGET_ROOT/.env"
if [ "$RESTORE_MODE" = "apply" ]; then
  guard_env_overwrite "$ENV_TARGET"
fi
if grep -qE "^${APP_TOPDIR}/\.env\$" <<< "$APP_LISTING"; then
  do_or_plan bash -c "tar -xzO -f '$RESTORE_ARCHIVE' ./app.tar.gz | tar -xz -O '${APP_TOPDIR}/.env' > '$ENV_TARGET' && chmod 0600 '$ENV_TARGET'"
  log_info ".env: $( [ "$RESTORE_MODE" = apply ] && echo "restored to $ENV_TARGET (mode 0600)" || echo "would be restored to $ENV_TARGET (mode 0600)" ) -- value NOT logged"
else
  log_error "app.tar.gz does not contain ${APP_TOPDIR}/.env -- cannot proceed without it (DB_PASSWORD, SECRET_KEY, etc. all live there)."
  exit 1
fi

# ---- media/: extract the whole subtree, EXCLUDING album_art_cache
#      (already excluded from the backup itself, but the exclusion is
#      re-asserted here defensively in case a future backup version
#      changes that). Only proceeds if media/ is actually present --
#      not every backup necessarily has one. --------------------------
if grep -qE "^${APP_TOPDIR}/media/" <<< "$APP_LISTING"; then
  MEDIA_TARGET="$RESTORE_TARGET_ROOT/media"
  do_or_plan bash -c "mkdir -p '$MEDIA_TARGET' && tar -xzO -f '$RESTORE_ARCHIVE' ./app.tar.gz | tar -xz -C '$RESTORE_TARGET_ROOT' --strip-components=1 --exclude='*/media/album_art_cache/*' '${APP_TOPDIR}/media'"
  log_info "media/: $( [ "$RESTORE_MODE" = apply ] && echo restored || echo "would be restored" ) to $MEDIA_TARGET (album_art_cache excluded -- regenerable)"
else
  log_warn "app.tar.gz has no media/ subtree -- nothing to restore there (may be legitimate for a station with no UI Theme uploads)."
fi

log_info "Nothing else was extracted from app.tar.gz -- code comes from the Git checkout above, not the tarball. See deploy/restore/README.md."
log_info "20-application: $( [ "$RESTORE_MODE" = apply ] && echo PASS || echo "PLAN complete" )"
