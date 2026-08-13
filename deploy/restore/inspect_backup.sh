#!/usr/bin/env bash
# deploy/restore/inspect_backup.sh -- IsadoraAir 1.2 Phase 4.
#
# Validates an IsadoraAir backup archive (as produced by
# deploy/backup_isadoraair.sh) WITHOUT extracting it to disk and without
# touching any live system state. Entirely read-only: every check below
# streams from the archive via `tar -xzO` (extract-to-stdout) rather than
# writing anything to a working directory, so this is always safe to run
# against a real production backup, including on the live host.
#
# Intended to be the first command run against any backup archive,
# whether during Phase 4's own staging validation or a real future
# emergency (see Phase 4 spec section 33) -- called automatically by
# 00-preflight.sh before any restore stage runs, but also fully useful
# stand-alone:
#
#   deploy/restore/inspect_backup.sh /path/to/isadoraair-backup-*.tar.gz
#
# Exit code: 0 if every REQUIRED check passes (warnings on OPTIONAL
# content -- StereoTool profile, station content, reports -- never fail
# the run, since a legitimate backup can have zero of any of those, e.g.
# a station with no operator-recorded voicetracks yet). Non-zero and a
# clearly labeled FAIL line if any required check fails -- "fail early
# and clearly", per the Phase 4 spec.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

usage() {
  cat >&2 <<'EOF'
Usage: inspect_backup.sh <path-to-backup-archive.tar.gz>

Read-only validation of an IsadoraAir backup archive's structure. Never
extracts to disk, never touches any live system. See this script's own
header comment for exactly what is checked.
EOF
}

if [ $# -lt 1 ]; then
  usage
  exit 2
fi
ARCHIVE="$1"

require_cmd tar

FAIL=0
NOTE_GIT_SHA=""
NOTE_BACKUP_SCRIPT_VERSION=""
NOTE_CREATED=""

pass() { printf '%-28s PASS  %s\n' "$1" "${2:-}"; }
warn() { printf '%-28s WARN  %s\n' "$1" "${2:-}"; }
fail() { printf '%-28s FAIL  %s\n' "$1" "${2:-}"; FAIL=1; }

echo "Inspecting: $ARCHIVE"
echo

# ---- 1. Archive exists -------------------------------------------------
if [ ! -f "$ARCHIVE" ]; then
  fail "Archive exists" "no such file"
  echo
  echo "OVERALL: FAIL -- cannot proceed, archive does not exist."
  exit 1
fi
pass "Archive exists" "$(du -h "$ARCHIVE" | cut -f1)"

# ---- 2. Outer tar is readable ------------------------------------------
LISTING=""
if ! LISTING=$(tar -tzf "$ARCHIVE" 2>&1); then
  fail "Outer tar readable" "tar -tzf failed -- archive is truncated or corrupt"
  echo
  echo "OVERALL: FAIL -- archive fails its own integrity check, cannot inspect further."
  exit 1
fi
pass "Outer tar readable" "$(printf '%s\n' "$LISTING" | wc -l) entries"

# Helper: does the outer archive contain this exact top-level entry?
#
# Uses a herestring (<<<), NOT `printf ... | grep -q`, on purpose --
# grep -q exits the instant it finds a match, and for a large enough
# LISTING that happens before the producer side of a pipe has finished
# writing, which delivers it SIGPIPE; under this script's own `set -o
# pipefail` (inherited from lib.sh) that SIGPIPE becomes the pipeline's
# reported exit status even though grep itself matched successfully --
# turning a real match into a false "no match" here. This is the exact
# gotcha deploy/backup_isadoraair.sh's own APP_TAR_LISTING check already
# hit and documented; a herestring avoids it (bash feeds it via a real
# temp file, not a live pipe a downstream grep -q can SIGPIPE), matching
# that script's own established fix. Caught here by this script's own
# validation against a real archive during Phase 4 -- see this repo's
# Phase 4 completion report.
has_entry() {
  # Entries are written as "./NAME" (backup_isadoraair.sh tars the
  # workdir with `tar czf ... -C "$WORKDIR" .`) -- match both forms so
  # this stays correct even if a future script version stops using -C.
  grep -qE "^(\./)?$1\$" <<< "$LISTING"
}

# ---- 3. MANIFEST.txt ----------------------------------------------------
if has_entry "MANIFEST.txt"; then
  MANIFEST_CONTENT=$(tar -xzO -f "$ARCHIVE" ./MANIFEST.txt 2>/dev/null || tar -xzO -f "$ARCHIVE" MANIFEST.txt 2>/dev/null || true)
  if [ -z "$MANIFEST_CONTENT" ]; then
    fail "MANIFEST.txt" "listed but could not be extracted"
  else
    pass "MANIFEST.txt" "present"
    # grep here (no -q, no early exit) is not subject to the SIGPIPE/
    # pipefail gotcha explained on has_entry() above -- plain `grep`
    # reads its entire input before exiting either way. Piping is fine.
    NOTE_GIT_SHA=$(printf '%s\n' "$MANIFEST_CONTENT" | grep -E '^IsadoraAir Git SHA:' | sed -E 's/^IsadoraAir Git SHA:\s*//' || true)
    NOTE_BACKUP_SCRIPT_VERSION=$(printf '%s\n' "$MANIFEST_CONTENT" | grep -E '^Backup script version:' | sed -E 's/^Backup script version:\s*//' || true)
    NOTE_CREATED=$(printf '%s\n' "$MANIFEST_CONTENT" | grep -E '^Created \(UTC\):' | sed -E 's/^Created \(UTC\):\s*//' || true)
  fi
else
  fail "MANIFEST.txt" "missing from archive"
fi

# ---- 4. database.dump: exists + looks like PG custom format ------------
if has_entry "database\\.dump"; then
  # pg_dump -Fc always begins with the 5-byte magic "PGDMP" -- checked
  # by streaming just the first 5 bytes out, never extracting the whole
  # (potentially multi-GB) dump to disk.
  MAGIC=$(tar -xzO -f "$ARCHIVE" ./database.dump 2>/dev/null | head -c 5 || true)
  if [ "$MAGIC" = "PGDMP" ]; then
    pass "Database dump" "PGDMP custom-format magic confirmed"
  else
    fail "Database dump" "present but does not start with PGDMP -- not a pg_dump -Fc file (got: ${MAGIC:-empty})"
  fi
else
  fail "Database dump" "database.dump missing from archive"
fi

# ---- 5/6. app.tar.gz: exists, contains manage.py + .env -----------------
if has_entry "app\\.tar\\.gz"; then
  pass "Application archive" "app.tar.gz present"
  APP_LISTING=$(tar -xzO -f "$ARCHIVE" ./app.tar.gz 2>/dev/null | tar -tz 2>/dev/null || true)
  if [ -z "$APP_LISTING" ]; then
    fail "app.tar.gz readable" "could not list contents (corrupt or empty)"
  else
    # Matched as "*/manage.py" (one path component deep, whatever the
    # top-level directory is named) rather than hardcoding "isadoraair/"
    # -- backup_isadoraair.sh names it after PROJECT_DIR's basename,
    # which is a deploy-time choice, not a fixed constant.
    #
    # Herestrings (<<<), not `printf ... | grep -q` -- see has_entry()'s
    # comment above for why: APP_LISTING is large (100+ KB for a real
    # archive) and every one of these uses grep -q, so the early-exit/
    # SIGPIPE/pipefail false-negative is very much live here, not
    # theoretical -- it was caught by exactly this check, against a real
    # archive, during this script's own Phase 4 validation pass.
    if grep -qE '^[^/]+/manage\.py$' <<< "$APP_LISTING"; then
      pass "app.tar.gz: manage.py" "present"
    else
      fail "app.tar.gz: manage.py" "missing -- application tree was not captured (see backup script's own symlink-dereference safeguard)"
    fi
    if grep -qE '^[^/]+/\.env$' <<< "$APP_LISTING"; then
      pass "app.tar.gz: .env" "present"
    else
      fail "app.tar.gz: .env" "missing -- cannot recover DB/SECRET_KEY without it"
    fi
    if grep -qE '^[^/]+/\.env\.(bak|lock)$' <<< "$APP_LISTING"; then
      warn "app.tar.gz: .env.bak/.lock" "present -- should have been excluded by the backup script; not fatal to a restore, but investigate why the exclude flags didn't take effect"
    fi
  fi
else
  fail "Application archive" "app.tar.gz missing from archive"
fi

# ---- 7. Optional/conditional payloads -----------------------------------
# None of these are hard requirements -- a legitimate backup can
# legitimately have zero StereoTool profiles, zero station content, or
# no reports directory yet (see backup_isadoraair.sh's own conditionals).
check_optional_dir() {
  local label="$1" pattern="$2"
  local count
  count=$(printf '%s\n' "$LISTING" | grep -cE "^(\./)?${pattern}" || true)
  if [ "$count" -gt 0 ]; then
    pass "$label" "$count entries"
  else
    warn "$label" "no entries found -- may be legitimate (see backup script's own conditionals) or may indicate the backup ran on a host missing this content"
  fi
}

check_optional_dir "nginx config (etc-live)"       'etc-live/isadoraair\.nginx$'
check_optional_dir "nginx snippet (etc-live)"      'etc-live/isadoraair-locations\.conf$'
check_optional_dir "systemd units (etc-live)"      'etc-live/isadoraair-.*\.service$'
check_optional_dir "StereoTool unit (etc-live)"    'etc-live/stereotool\.service$'
check_optional_dir "StereoTool profile (.sts)"     'stereotool/.*\.sts$'
check_optional_dir "Station content (srv-content)" 'srv-content/'
check_optional_dir "Reports"                       'reports/'

echo
if [ -n "$NOTE_GIT_SHA" ]; then
  echo "Git SHA:              $NOTE_GIT_SHA"
fi
if [ -n "$NOTE_BACKUP_SCRIPT_VERSION" ]; then
  echo "Backup script version: $NOTE_BACKUP_SCRIPT_VERSION"
fi
if [ -n "$NOTE_CREATED" ]; then
  echo "Created (UTC):        $NOTE_CREATED"
fi
echo

if [ "$FAIL" -ne 0 ]; then
  echo "OVERALL: FAIL -- one or more required checks failed. Do not proceed with a restore using this archive."
  exit 1
fi
echo "OVERALL: PASS -- archive structure is valid. Required content present."
