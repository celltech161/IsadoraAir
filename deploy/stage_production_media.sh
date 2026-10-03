#!/usr/bin/env bash
# deploy/stage_production_media.sh -- stage the DURABLE part of the iPortal
# production-media store for the nightly backup (deploy/backup_isadoraair.sh).
#
# Usage:
#   stage_production_media.sh <PRODUCTION_MEDIA_ROOT> <destination-directory>
#
# Copies <root>/media/ -- the permanent, immutable ProductionMedia bytes -- to
# <destination>/production-media/media/, preserving modes and timestamps.
#
# NOTHING else under the root is ever named as a copy source, so exclusion is
# structural, not a filter that could drift:
#   incoming/   partial uploads        transient, never backed up
#   work/       processing scratch     transient, never backed up
#   locks/      lock sidecars          transient, never backed up
# After a restore these are recreated empty by deploy/restore/40-station-
# content.sh and `manage.py production_reconcile` reclaims anything stale.
#
# Kept as its own small, dependency-free helper (like
# encrypt_recovery_credentials.sh) so the exact inclusion/exclusion rule can
# be executed in tests without production secrets or a network.
#
# Exit status: 0 copied, or nothing to copy (no store yet is legitimate);
#              2 bad usage / unsafe path; 3 media/ exists but is not a real
#              directory (misconfiguration: fail closed rather than silently
#              skip durable content).
set -euo pipefail

if [ $# -ne 2 ]; then
  echo "usage: $0 <PRODUCTION_MEDIA_ROOT> <destination-directory>" >&2
  exit 2
fi
ROOT="$1"
DEST="$2"

case "$ROOT" in
  /*) ;;
  *) echo "error: production media root must be an absolute path: $ROOT" >&2; exit 2 ;;
esac
case "/$ROOT/" in
  */../*) echo "error: production media root must not contain '..': $ROOT" >&2; exit 2 ;;
esac
if [ ! -d "$DEST" ]; then
  echo "error: destination directory does not exist: $DEST" >&2
  exit 2
fi

SRC="$ROOT/media"
if [ ! -e "$SRC" ] && [ ! -L "$SRC" ]; then
  echo "  (no production media found at $SRC)"
  exit 0
fi
if [ -L "$SRC" ] || [ ! -d "$SRC" ]; then
  echo "error: $SRC is not a real directory; refusing to guess what to back up" >&2
  exit 3
fi

TARGET="$DEST/production-media"
if [ -e "$TARGET/media" ]; then
  echo "error: $TARGET/media already exists in the staging area" >&2
  exit 2
fi
mkdir -p "$TARGET"
cp -a "$SRC" "$TARGET/media"
COUNT=$(find "$TARGET/media" -type f | wc -l)
echo "  ${COUNT} file(s), $(du -sh "$TARGET/media" 2>/dev/null | cut -f1) of production media (media/ only; incoming/, work/, locks/ are not backed up)"
