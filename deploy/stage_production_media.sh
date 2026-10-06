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
# The root is validated by production/root_policy.py -- the SAME policy the
# Django runtime and the restore tooling use -- before anything is read: it
# must be a dedicated production-media directory (never /, a system tree, a
# broad anchor such as /srv or /var, station content, or code). Extra protected
# paths may be passed as further arguments (the backup script passes the
# station content roots from .env, the application root and its own workdir).
#
# Capacity (2.22B): the copy, and then the final archive built from it, land
# on the destination's filesystem (the backup's /tmp work area -- RAM-backed
# tmpfs on a station). Before copying anything, this helper requires
#   available >= 2 x (bytes under media/) + reserve
# (the staged copy plus its share of the archive -- audio barely compresses --
# plus PRODUCTION_MEDIA_STAGING_RESERVE_BYTES, default 1 GiB, for everything
# else the backup stages) and refuses otherwise, BEFORE writing a byte, so a
# growing store can never fill the work area part-way through a backup and
# starve the station. The refusal names the media size, the requirement, the
# available space and the filesystem. The reserve must be 0..1 TiB; every
# count is bounded (< 10^18) so the arithmetic cannot overflow, and a failed
# or implausible du/df answer refuses rather than guesses.
#
# Exit status: 0 copied, or nothing to copy (no store yet is legitimate);
#              2 bad usage or a bad reserve; 3 media/ exists but is not a real directory, or
#              the root fails the safety policy (misconfiguration: fail closed
#              rather than silently skip durable content or read the wrong tree);
#              4 not enough staging space, or it cannot be measured (nothing was copied).
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: $0 <PRODUCTION_MEDIA_ROOT> <destination-directory> [--protected PATH ...]" >&2
  exit 2
fi
ROOT="$1"
DEST="$2"
shift 2
EXTRA_PROTECTED=()
while [ $# -gt 0 ]; do
  case "$1" in
    --protected) EXTRA_PROTECTED+=(--protected "${2:?--protected needs a path}"); shift 2 ;;
    *) echo "usage: $0 <PRODUCTION_MEDIA_ROOT> <destination-directory> [--protected PATH ...]" >&2; exit 2 ;;
  esac
done

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
TOOLING_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
POLICY="$TOOLING_ROOT/production/root_policy.py"
if ! python3 "$POLICY" check --root "$ROOT" --dedicated --app-root "$TOOLING_ROOT" "${EXTRA_PROTECTED[@]}" >/dev/null; then
  echo "error: refusing to back up from an unsafe production media root: $ROOT" >&2
  exit 3
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

# ---- capacity preflight (before ANY write) -----------------------------------
# Overflow-safe by construction: every byte count must be a plain decimal of
# at most 18 digits (< 10^18), so 2 x media + reserve < 2^63 always holds in
# bash's signed 64-bit arithmetic, and every value is read base-10 (10#) so a
# leading zero is never octal. Anything else fails closed BEFORE staging.
MAX_RESERVE_BYTES=1099511627776       # 1 TiB: the largest accepted reserve
is_byte_count() { [[ "$1" =~ ^[0123456789]{1,18}$ ]]; }   # ASCII digits, whatever the locale

RESERVE_RAW="${PRODUCTION_MEDIA_STAGING_RESERVE_BYTES:-1073741824}"
if ! is_byte_count "$RESERVE_RAW" || (( 10#$RESERVE_RAW > MAX_RESERVE_BYTES )); then
  echo "error: PRODUCTION_MEDIA_STAGING_RESERVE_BYTES must be a whole number of bytes from 0 to ${MAX_RESERVE_BYTES} (got '${RESERVE_RAW}'); refusing to stage" >&2
  exit 2
fi
RESERVE_BYTES=$(( 10#$RESERVE_RAW ))

if ! DU_OUT=$(du -s -B1 --apparent-size -- "$SRC"); then
  echo "error: could not measure production media at $SRC; refusing to stage" >&2
  exit 4
fi
MEDIA_RAW=${DU_OUT%%[[:space:]]*}
if ! is_byte_count "$MEDIA_RAW"; then
  echo "error: implausible production media size '${MEDIA_RAW}' at $SRC; refusing to stage" >&2
  exit 4
fi
MEDIA_BYTES=$(( 10#$MEDIA_RAW ))
REQUIRED_BYTES=$(( MEDIA_BYTES * 2 + RESERVE_BYTES ))

if ! DF_OUT=$(df -P -B1 -- "$DEST"); then
  echo "error: could not determine free space at $DEST; refusing to stage" >&2
  exit 4
fi
AVAILABLE_RAW=$(printf '%s\n' "$DF_OUT" | awk 'NR == 2 { print $4 }')
STAGING_FS=$(printf '%s\n' "$DF_OUT" | awk 'NR == 2 { print $6 }')
if ! is_byte_count "$AVAILABLE_RAW"; then
  echo "error: could not determine free space at $DEST (got '${AVAILABLE_RAW}'); refusing to stage" >&2
  exit 4
fi
AVAILABLE_BYTES=$(( 10#$AVAILABLE_RAW ))
if (( AVAILABLE_BYTES < REQUIRED_BYTES )); then
  echo "error: not enough staging space for production media: media/ holds ${MEDIA_BYTES} bytes, staging it needs ${REQUIRED_BYTES} bytes (2 x media + ${RESERVE_BYTES} reserve) but only ${AVAILABLE_BYTES} bytes are available on ${STAGING_FS} (${DEST}). Nothing was copied. Free space there, or move the backup work area to a larger filesystem, before the next backup." >&2
  exit 4
fi
echo "  staging-space check: media ${MEDIA_BYTES} bytes, need ${REQUIRED_BYTES}, available ${AVAILABLE_BYTES} on ${STAGING_FS}"

TARGET="$DEST/production-media"
if [ -e "$TARGET/media" ]; then
  echo "error: $TARGET/media already exists in the staging area" >&2
  exit 2
fi
mkdir -p "$TARGET"
cp -a "$SRC" "$TARGET/media"
COUNT=$(find "$TARGET/media" -type f | wc -l)
echo "  ${COUNT} file(s), $(du -sh "$TARGET/media" 2>/dev/null | cut -f1) of production media (media/ only; incoming/, work/, locks/ are not backed up)"
