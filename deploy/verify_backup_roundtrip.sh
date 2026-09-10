#!/usr/bin/env bash
# IsadoraAir 1.2 Phase 6 -- weekly REAL remote backup round-trip verifier.
#
# Nightly backups (deploy/backup_isadoraair.sh) prove the archive was
# buildable and uploadable at the moment it was made. This script is the
# recurring proof that the promoted remote object is STILL actually
# there, byte-identical to what the receipt says was uploaded, and
# structurally/catalog-valid -- the only way to catch remote-side
# corruption, an accidental deletion, or a retention-prune bug before a
# real disaster forces the question.
#
# Deliberately does NOT parse an SFTP directory listing to guess the
# newest backup -- it downloads the EXACT object last-success.json (see
# isadoraair/backup_assurance.py) says was promoted, and requires its
# SHA256 to match exactly. This also means a run of this script can
# never be fooled by a newer, not-yet-verified backup that happens to
# be sitting on the remote -- it only ever re-proves the specific
# object the last successful nightly run actually promoted.
#
# Uses the SAME credential file and secret-handling convention as
# backup_isadoraair.sh: ~/.iasboxbu.cred, sshpass -e (SSHPASS env var,
# never argv). The remote password never appears in argv, logs, or any
# receipt this script writes.
#
# Read-only against the remote and against the local IsadoraAir git
# repository: never deletes/mutates the remote archive, never restores/
# mutates any database, never writes anything outside its own private
# mode-0700 temporary directory (removed on every exit, success or
# failure).
#
# Does NOT implement a second exact release-introduction/canonical-
# release resolver -- P1 1.16 owns that boundary. Here, exact receipt/
# archive Git SHA agreement plus "that commit exists locally and is an
# ancestor of canonical main" is sufficient recurring assurance.
#
# DRY_RUN is not offered here on purpose -- unlike the nightly backup
# (which builds a large, real local archive either way), every step
# this script performs beyond reading receipts is exactly the real
# round-trip it exists to prove; there is no cheaper "real path,
# skip only the network part" variant that would still prove anything.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ASSURANCE_MODULE="$REPO_ROOT/isadoraair/backup_assurance.py"
INSPECT_SCRIPT="$SCRIPT_DIR/restore/inspect_backup.sh"

CONFIG_FILE="$HOME/.iasboxbu.cred"
# The checkout whose git history is checked for ancestry -- same
# convention/default as backup_isadoraair.sh's own PROJECT_DIR.
PROJECT_DIR="${PROJECT_DIR:-/opt/isadoraair}"
SFTP_RETRIES=3
SFTP_RETRY_DELAY=5

# Naming contract every promoted nightly archive must satisfy (matches
# backup_isadoraair.sh's own REMOTE_FILE pattern exactly) -- the receipt
# is trusted for byte-identity (SHA256), but its FILENAME is still
# validated against this fixed contract before it is ever used to build
# a remote path, so a corrupted/hand-edited receipt can't smuggle an
# unexpected remote path through this script.
REMOTE_FILENAME_RE='^isadoraair-backup-[0-9]{8}-[0-9]{6}\.tar\.gz$'

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Error: required command not found: $1" >&2
    exit 1
  fi
}

require_cmd sftp
require_cmd sshpass
require_cmd sha256sum
require_cmd tar
require_cmd pg_restore
require_cmd python3
require_cmd git

if [ ! -f "$CONFIG_FILE" ]; then
  echo "Error: credential file $CONFIG_FILE not found." >&2
  exit 1
fi
# shellcheck disable=SC1090
. "$CONFIG_FILE"
: "${BAK_HOST:?BAK_HOST not set in $CONFIG_FILE}"
: "${BAK_USER:?BAK_USER not set in $CONFIG_FILE}"
: "${BAK_PATH:?BAK_PATH not set in $CONFIG_FILE}"
: "${BAK_PASS:?BAK_PASS not set in $CONFIG_FILE}"
BAK_PORT="${BAK_PORT:-22}"
export SSHPASS="$BAK_PASS"

sftp_run() {
  local batch attempt=1
  batch="$(cat)"
  while true; do
    if output=$(sshpass -e sftp -P "$BAK_PORT" "${BAK_USER}@${BAK_HOST}" <<< "$batch" 2>&1); then
      printf '%s\n' "$output"
      return 0
    fi
    if [ "$attempt" -ge "$SFTP_RETRIES" ]; then
      printf '%s\n' "$output" >&2
      return 1
    fi
    echo "  sftp attempt ${attempt} failed, retrying in ${SFTP_RETRY_DELAY}s..." >&2
    attempt=$((attempt + 1))
    sleep "$SFTP_RETRY_DELAY"
  done
}

json_get() {
  python3 -c "
import json, sys
data = json.loads(sys.argv[1])
for key in sys.argv[2].split('.'):
    data = (data or {}).get(key) if isinstance(data, dict) else None
print('' if data is None else data)
" "$1" "$2"
}

TMPDIR=""
ATTEMPT_ID=""
CURRENT_STAGE="starting"
REMOTE_FILENAME=""
BACKUP_GIT_SHA=""

# Every downloaded/extracted artifact lives ONLY under this private,
# mode-0700 temporary directory, removed on EVERY exit path (success,
# failure, or an unexpected error) -- never left behind for a human to
# clean up, and never reused across runs.
cleanup() {
  local exit_code=$?
  if [ -n "$ATTEMPT_ID" ]; then
    if [ "$exit_code" -eq 0 ]; then
      python3 "$ASSURANCE_MODULE" roundtrip-attempt-finish \
        --attempt-id "$ATTEMPT_ID" --outcome success --stage complete \
        --remote-filename "$REMOTE_FILENAME" --backup-git-sha "$BACKUP_GIT_SHA" \
        >/dev/null 2>&1 || echo "Warning: failed to record round-trip success attempt receipt." >&2
    else
      python3 "$ASSURANCE_MODULE" roundtrip-attempt-finish \
        --attempt-id "$ATTEMPT_ID" --outcome failed --stage "$CURRENT_STAGE" --exit-code "$exit_code" \
        --remote-filename "$REMOTE_FILENAME" --backup-git-sha "$BACKUP_GIT_SHA" \
        >/dev/null 2>&1 || echo "Warning: failed to record round-trip failure attempt receipt." >&2
    fi
  fi
  if [ -n "$TMPDIR" ]; then
    rm -rf "$TMPDIR"
  fi
  exit "$exit_code"
}
trap cleanup EXIT

ATTEMPT_ID=$(python3 "$ASSURANCE_MODULE" roundtrip-attempt-start --stage "$CURRENT_STAGE") || ATTEMPT_ID=""
if [ -z "$ATTEMPT_ID" ]; then
  echo "Error: could not record a round-trip 'running' receipt -- aborting." >&2
  exit 1
fi

echo "IsadoraAir weekly backup round-trip verification starting..."

# ---- 1/2/3. Read + validate last-success.json --------------------------
CURRENT_STAGE="reading_receipt"
SUCCESS_JSON=$(python3 "$ASSURANCE_MODULE" read-last-success) || {
  echo "Error: last-success.json exists but is malformed -- cannot verify (fail closed)." >&2
  exit 1
}

if [ -z "$SUCCESS_JSON" ]; then
  echo "Error: no valid last-success.json found -- nothing to verify yet (run the nightly backup, or check ${CONFIG_FILE%/*}/.local/state/isadoraair/backup-assurance for a malformed receipt)." >&2
  exit 1
fi

REMOTE_FILENAME=$(json_get "$SUCCESS_JSON" remote_filename)
EXPECTED_SHA256=$(json_get "$SUCCESS_JSON" archive_sha256)
BACKUP_GIT_SHA=$(json_get "$SUCCESS_JSON" git_sha)

if [ -z "$REMOTE_FILENAME" ] || [ -z "$EXPECTED_SHA256" ] || [ -z "$BACKUP_GIT_SHA" ]; then
  echo "Error: last-success.json is missing remote_filename/archive_sha256/git_sha -- cannot verify." >&2
  exit 1
fi

# ---- 3. Strict filename-contract validation -----------------------------
if ! [[ "$REMOTE_FILENAME" =~ $REMOTE_FILENAME_RE ]]; then
  echo "Error: last-success.json's remote_filename '${REMOTE_FILENAME}' does not match the IsadoraAir backup naming contract -- refusing to use it to build a remote path." >&2
  exit 1
fi

echo "  Verifying: ${REMOTE_FILENAME}"

# ---- 4. Download the exact promoted object into a private temp dir -----
CURRENT_STAGE="downloading"
TMPDIR=$(mktemp -d)
chmod 0700 "$TMPDIR"
LOCAL_ARCHIVE="$TMPDIR/${REMOTE_FILENAME}"

echo "Downloading the exact promoted object via SFTP..."
if ! { echo "cd ${BAK_PATH}"; echo "get ${REMOTE_FILENAME} ${LOCAL_ARCHIVE}"; echo "bye"; } | sftp_run > /dev/null; then
  echo "Error: could not download ${REMOTE_FILENAME} from the remote -- see stderr above." >&2
  exit 1
fi
if [ ! -s "$LOCAL_ARCHIVE" ]; then
  echo "Error: downloaded archive is empty or missing." >&2
  exit 1
fi

# ---- 5. SHA256 must match the receipt exactly ---------------------------
CURRENT_STAGE="hash_verify"
OBSERVED_SHA256=$(sha256sum "$LOCAL_ARCHIVE" | cut -d' ' -f1)
if [ "$OBSERVED_SHA256" != "$EXPECTED_SHA256" ]; then
  echo "Error: downloaded archive's SHA256 does not match last-success.json -- possible remote corruption or tampering." >&2
  exit 1
fi
echo "  SHA256 matches last-success.json."

# ---- 6. Authoritative structural inspector ------------------------------
CURRENT_STAGE="inspector"
echo "Running the authoritative archive inspector..."
if ! "$INSPECT_SCRIPT" "$LOCAL_ARCHIVE" > "$TMPDIR/inspect.out" 2>&1; then
  echo "Error: inspect_backup.sh reported a structural FAIL -- see below." >&2
  cat "$TMPDIR/inspect.out" >&2
  exit 1
fi
echo "  inspect_backup.sh: OVERALL PASS"

# ---- 7. pg_restore --list against database.dump, extracted privately ---
CURRENT_STAGE="pg_restore_list"
echo "Verifying the database dump catalog is readable..."
if ! tar -xzO -f "$LOCAL_ARCHIVE" ./database.dump > "$TMPDIR/database.dump" 2>/dev/null \
   && ! tar -xzO -f "$LOCAL_ARCHIVE" database.dump > "$TMPDIR/database.dump" 2>/dev/null; then
  echo "Error: could not extract database.dump from the downloaded archive." >&2
  exit 1
fi
chmod 0600 "$TMPDIR/database.dump"
if ! pg_restore --list "$TMPDIR/database.dump" > /dev/null; then
  echo "Error: pg_restore --list could not read the downloaded archive's database dump catalog." >&2
  exit 1
fi
echo "  ok"

# ---- 8/9. Archive manifest Git SHA must match the receipt, and that ----
#           commit must exist locally and be an ancestor of main --------
CURRENT_STAGE="git_sha_match"
MANIFEST_CONTENT=$(tar -xzO -f "$LOCAL_ARCHIVE" ./MANIFEST.txt 2>/dev/null || tar -xzO -f "$LOCAL_ARCHIVE" MANIFEST.txt 2>/dev/null || true)
ARCHIVE_GIT_SHA=$(printf '%s\n' "$MANIFEST_CONTENT" | grep -E '^IsadoraAir Git SHA:' | sed -E 's/^IsadoraAir Git SHA:\s*//' || true)
if [ -z "$ARCHIVE_GIT_SHA" ] || [ "$ARCHIVE_GIT_SHA" = "unknown" ]; then
  echo "Error: archive's MANIFEST.txt has no usable Git SHA -- cannot verify provenance." >&2
  exit 1
fi
if [ "$ARCHIVE_GIT_SHA" != "$BACKUP_GIT_SHA" ]; then
  echo "Error: archive manifest's Git SHA (${ARCHIVE_GIT_SHA}) does not match last-success.json's recorded Git SHA (${BACKUP_GIT_SHA})." >&2
  exit 1
fi

CURRENT_STAGE="main_ancestry"
if ! git -C "$PROJECT_DIR" cat-file -e "${BACKUP_GIT_SHA}^{commit}" 2>/dev/null; then
  echo "Error: Git SHA ${BACKUP_GIT_SHA} does not exist in the local IsadoraAir repository (${PROJECT_DIR}) -- cannot verify it is an ancestor of main." >&2
  exit 1
fi
MAIN_REF="main"
if ! git -C "$PROJECT_DIR" rev-parse --verify -q "$MAIN_REF" > /dev/null; then
  MAIN_REF="origin/main"
fi
if ! git -C "$PROJECT_DIR" rev-parse --verify -q "$MAIN_REF" > /dev/null; then
  echo "Error: neither local 'main' nor 'origin/main' could be resolved in ${PROJECT_DIR} -- cannot verify ancestry." >&2
  exit 1
fi
if ! git -C "$PROJECT_DIR" merge-base --is-ancestor "$BACKUP_GIT_SHA" "$MAIN_REF"; then
  echo "Error: Git SHA ${BACKUP_GIT_SHA} is not an ancestor of canonical ${MAIN_REF}." >&2
  exit 1
fi
echo "  Git SHA ${BACKUP_GIT_SHA} verified: exists locally, is an ancestor of ${MAIN_REF}."

# ---- Everything passed -- record the round-trip success receipt -------
CURRENT_STAGE="record_success"
python3 "$ASSURANCE_MODULE" roundtrip-record-success \
  --remote-filename "$REMOTE_FILENAME" \
  --expected-sha256 "$EXPECTED_SHA256" \
  --observed-sha256 "$OBSERVED_SHA256" \
  --backup-git-sha "$BACKUP_GIT_SHA" \
  --inspector-result pass \
  --pg-restore-catalog-result pass \
  --main-ancestry-result pass

echo
echo "IsadoraAir weekly backup round-trip verification PASSED."
