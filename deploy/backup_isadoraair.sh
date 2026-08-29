#!/usr/bin/env bash
# IsadoraAir backup — replaces the old backup_izzy.sh, which predated the
# current Django rewrite (2 of its 3 source paths, /etc/isadoraair and
# /home/jreed/isadoraair-dev, no longer exist) and never dumped the
# database at all — meaningless for a project that now stores everything
# that matters (Track/Category/Rotation/Playlist/ScheduleBlock/RBDSConfig/
# MonitorCheck/...) in PostgreSQL rather than flat files.
#
# What this backs up, into one timestamped tar.gz:
#   - A full pg_dump (custom format) of the database.
#   - The application tree: code, .env, media/ (UI Theme logos etc.) —
#     everything except what's regenerable (venv/, staticfiles/,
#     __pycache__/), redundant with GitHub (.git/), or a stray local
#     .env.bak/.env.lock working file (see the 2026-08-12 note below --
#     .env itself is still fully included, just not its ad hoc editor/
#     lock-file siblings).
#   - The LIVE nginx site config and shared snippet, and the LIVE systemd
#     unit files for the core services — see the 2026-08-12 note below for
#     why these are captured live rather than assumed to match deploy/'s
#     reference copies.
#   - StereoTool's .sts processing profile(s), if present (NOT the
#     StereoTool binaries/license themselves — those are externally
#     reprovisioned, see docs/DISASTER_RECOVERY.md).
#   - Small, operator-created /srv/isadoraair content (FX Cart audio,
#     voicetracks) and royalty/SoundExchange report filings, if present.
#   - If configured (BACKUP_RECOVERY_AGE_RECIPIENT/_FILE): age-encrypted
#     copies of ~/.iasboxbu.cred, ~/.syndicated_ingest.cred, and
#     ~/.ogremote_ingest.cred, under recovery-credentials/*.age — see the
#     2026-08-18 note below and deploy/encrypt_recovery_credentials.sh.
#     Disabled by default; never included in plaintext either way.
#   - The station's CURRENT Runtime Foundation E7 disaster-recovery
#     payload (an offline-capable copy of the E3 Kokoro/Piper TTS bundle
#     and/or E4 native fdkaac source material), under runtime-recovery/
#     -- see the 2026-08-29 note below and docs/RUNTIME_BACKUP_PAYLOAD.md.
#     Included only if a current payload has been explicitly prepared
#     and activated on this host (isadoraair.runtime_recovery /
#     manage.py prepare_runtime_recovery_payload --activate); this step
#     never builds, downloads, or acquires anything itself -- it only
#     validates and copies an already-prepared, already-validated local
#     payload. If not yet configured, the archive is built exactly as
#     before Runtime Foundation E7B existed (no new failure mode) unless
#     BACKUP_REQUIRED_RECOVERY_COMPONENTS says otherwise -- see that note.
#
# Deliberately excluded — see docs/DISASTER_RECOVERY.md for the full,
# explicit policy this was derived from:
#   - /srv/isadoraair/music (717+ GB, not this box's only copy; storage
#     resilience for the library itself is a separate future task, not
#     part of this application/config/database backup)
#   - /srv/isadoraair/waveforms (regenerable via `manage.py analyze_tracks`)
#   - /srv/isadoraair/aircheck, rip_staging (high-growth/transient — own
#     retention policy, not core DR state)
#   - /srv/isadoraair/mitd_artbell and similar large syndicated-show
#     staging trees (own future sizing decision, not small/medium state)
#
# 2026-08-12 disaster-recovery Phase 2: this script itself used to exist
# ONLY on the host (~/bin/backup_isadoraair.sh), absent from the repo
# entirely — a real DR gap, since losing the host meant losing the
# backup mechanism's own source alongside everything it was protecting.
# This repo copy is now the authoritative implementation. Also closes two
# concrete Phase-1-audit gaps:
#   - nginx: the OLD version of this script backed up
#     /etc/nginx/sites-available/isadoraair, which had silently diverged
#     from the file actually serving traffic,
#     /etc/nginx/sites-enabled/isadoraair (a plain file, not the symlink
#     Debian's convention assumes — see deploy/README.md's "One
#     authoritative nginx config" note). Phase 2 reconciled the live host
#     so sites-enabled is a real symlink to sites-available again, making
#     sites-available correct to back up — this script now also grabs the
#     shared snippets/isadoraair-locations.conf it `include`s, which
#     wasn't backed up at all before.
#   - StereoTool's .sts processing profile (station-specific on-air EQ/
#     loudness/AESMPX tuning) wasn't backed up at all before.
#
# 2026-08-12 follow-up: a DRY_RUN validation pass surfaced that the app
# archive step also swept up two pre-existing, untracked project-root
# files alongside the intended .env -- .env.bak (a stale hand-made copy,
# itself holding real secrets) and .env.lock (a lock file, not
# configuration). Neither is anything a restore needs; .env itself is
# still fully included. Both are now excluded explicitly.
#
# 2026-08-17 disaster-recovery Phase 4.5: the intended follow-up above
# has happened -- production's isadoraair-backup.service now runs THIS
# file directly (ExecStart=/opt/isadoraair/deploy/backup_isadoraair.sh,
# confirmed live). What Phase 4.5 actually found and fixed was that
# deploy/isadoraair-backup.service's own repo TEMPLATE had not been
# updated to match -- it still pointed at the old @@ISA_HOME@@/bin/
# path, meaning a fresh install rendering that template would install a
# unit pointing at a script the restore tooling never creates. See
# docs/DISASTER_RECOVERY.md's "Known deployment follow-up, resolved"
# section for the full history.
#
# 2026-08-18 disaster-recovery Phase 4.5 final follow-up: encrypted
# preservation of the three companion-project credential files
# (~/.iasboxbu.cred, ~/.syndicated_ingest.cred, ~/.ogremote_ingest.cred)
# that were previously deliberately excluded entirely (see "Secrets NOT
# included" in the manifest below). These now get an age-encrypted copy
# under recovery-credentials/*.age in the archive -- see
# deploy/encrypt_recovery_credentials.sh (the standalone helper this
# script calls for the actual encryption) for the full security model.
# In short: only a PUBLIC age recipient is ever configured on this host
# (BACKUP_RECOVERY_AGE_RECIPIENT/_FILE below); the matching PRIVATE key
# is never generated or stored here, so compromising this host or any
# backup archive it produces does not grant decryption capability.
# Disabled by default (neither env var set) -- a fresh generic install
# backs up exactly as before, with no new failure mode, until an
# operator deliberately opts in. Once opted in, failure is closed: see
# encrypt_recovery_credentials.sh's own header for the exact conditions
# that abort this backup before any upload. See
# docs/DISASTER_RECOVERY_RESTORE.md's "Encrypted recovery-credential
# preservation" section for the full operator-facing design and the
# manual decrypt-on-restore procedure (deliberately NOT automated here
# -- the private key must never be handled by anything running on this
# host).
#
# 2026-08-29 Runtime Foundation E7B: this is what makes the archive
# "backup v3" -- it can now carry a self-contained, offline-capable
# runtime recovery payload (Runtime Foundation E7A's own
# isadoraair.runtime_recovery contract: an E3 TTS bundle and/or E4
# native fdkaac source material) rather than requiring a restore to
# reconstruct Kokoro/Piper/fdkaac from the internet by hand. This
# script never builds or acquires that payload itself -- acquisition
# (isadoraair.runtime_recovery.RuntimeRecoveryBuilder, an explicit,
# operator-run `manage.py prepare_runtime_recovery_payload --apply`
# then `--activate`) stays a deliberately separate, out-of-band step;
# this script only validates the CURRENT already-prepared payload
# (manage.py validate_runtime_recovery_payload --current, the same
# read-only Python API a restore will later call) and, if it validates
# cleanly, copies it into the archive verbatim. See
# docs/RUNTIME_BACKUP_PAYLOAD.md for the full contract and the
# RECOVERY_PAYLOAD_ROOT / BACKUP_REQUIRED_RECOVERY_COMPONENTS env vars
# this step reads (both documented at their point of use below).
#
# Pushes the result via SFTP to a remote target configured in
# ~/.iasboxbu.cred (BAK_HOST, BAK_USER, BAK_PORT, BAK_PATH, BAK_PASS) —
# never hardcoded here; this file has no station-specific secrets in it.
#
# Also prunes remote isadoraair-backup-*.tar.gz files older than
# RETENTION_DAYS -- the date is parsed from the filename itself
# (YYYYMMDD, set at upload time), not the remote server's `ls -l` date
# column, which is formatted inconsistently across year boundaries
# ("Nov 20  2025" vs "Jul  8 02:20") and isn't reliably parseable.
#
# Real bug hit and fixed during original setup: opening two sftp sessions
# back-to-back (one to upload, a second right after to prune) got the
# second one "Connection reset" by this specific remote host, confirmed
# live -- the delete never ran. Fixed by listing the remote directory
# FIRST (before the slow pg_dump/tar work, giving the connections natural
# spacing) and combining the upload + all prune deletes into ONE final
# sftp session, plus a retry wrapper for general resilience against
# transient connection drops.
#
# DRY_RUN=1 (env var): builds the full archive from real production
# source paths exactly as normal, but skips ~/.iasboxbu.cred entirely and
# never opens an SFTP connection -- no remote listing, no upload, no
# prune. The finished archive is left at its local path (printed at the
# end) instead of being deleted, for exactly the kind of local
# tar -tzf/MANIFEST.txt verification pass this was added for. Nothing
# else about the archive-building logic changes -- this exercises the
# real backup path, not a separate one.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Version of this Git-owned backup implementation. Archive format is
# classified separately in runtime-recovery-archive.json: this script
# can produce a legacy/non-self-contained 2.1.0 archive before a station
# deliberately activates a policy-satisfying payload, or a self-contained
# 3.0.0 archive after it does. The implementation version must never
# ambiguously stand in for both archive classes. 2.1.0 added the optional
# recovery-credentials/*.age directory (additive, backward compatible --
# an archive with encryption disabled or absent is still a fully valid
# 2.0.0-shaped backup in every other respect). 3.0.0: added the optional
# runtime-recovery/ Runtime Foundation E7 disaster-recovery payload --
# a MAJOR implementation bump, because this enables closing the
# "restore has to reconstruct Kokoro/Piper/fdkaac from the internet by
# hand" DR gap this whole backup mechanism exists for; restore tooling
# classifies an archive without a policy-satisfying runtime-recovery/
# payload as legacy format 2.1.0, never as self-contained v3 -- see
# docs/RUNTIME_BACKUP_PAYLOAD.md's "Backward compatibility" section.
SCRIPT_VERSION="3.0.0"

# See the DRY_RUN note in the header comment above.
DRY_RUN="${DRY_RUN:-0}"

RETENTION_DAYS=30
SFTP_RETRIES=3
SFTP_RETRY_DELAY=5

CONFIG_FILE="$HOME/.iasboxbu.cred"
# Env-overridable, defaulting to the same /opt/isadoraair convention
# deploy/'s own systemd units use (see deploy/README.md's ISA_ROOT) --
# this script isn't sed-rendered by the install loop like the *.service/
# *.timer/*.conf files, so it takes its one station-specific path as a
# plain shell default instead.
PROJECT_DIR="${PROJECT_DIR:-/opt/isadoraair}"
STEREOTOOL_DIR="${STEREOTOOL_DIR:-$HOME/stereotool}"
# Runtime Foundation E7B -- see the 2026-08-29 header note above and
# docs/RUNTIME_BACKUP_PAYLOAD.md. RECOVERY_PAYLOAD_ROOT is WHERE to look
# for the station's current, already-prepared-and-activated recovery
# payload (never created or written to by this script -- see
# isadoraair.runtime_recovery.resolve_current_recovery_payload_root).
# BACKUP_REQUIRED_RECOVERY_COMPONENTS is empty/unset by default --
# exactly the same "disabled by default, no new failure mode until an
# operator deliberately opts in" contract the recovery-credential
# encryption feature above already established. Once set (a
# comma-separated list drawn from kokoro/piper/native_fdkaac -- see
# isadoraair.runtime_recovery.RECOVERY_POLICY_COMPONENT_NAMES), this
# backup becomes fail-closed: no current payload, or a current payload
# that does not positively contain every listed component, aborts
# before upload. This is the mechanism the 2026-08-29 note's
# "historical dormant-Kokoro" migration period needs: E1's own
# kokoro.required flag is NOT consulted here at all (see
# isadoraair/runtime_recovery.py's own module docstring for exactly
# why) -- an operator who knows this station's dedication/road-condition
# features still depend on the historical Kokoro path sets
# BACKUP_REQUIRED_RECOVERY_COMPONENTS=kokoro explicitly instead.
RECOVERY_PAYLOAD_ROOT="${RECOVERY_PAYLOAD_ROOT:-/var/lib/isadoraair/runtime-recovery}"
BACKUP_REQUIRED_RECOVERY_COMPONENTS="${BACKUP_REQUIRED_RECOVERY_COMPONENTS:-}"
STAMP=$(date +%Y%m%d-%H%M%S)
WORKDIR="/tmp/isadoraair-backup-${STAMP}"
TMP_TAR="/tmp/isadoraair-backup-${STAMP}.tar.gz"
REMOTE_FILE="isadoraair-backup-${STAMP}.tar.gz"
# Uploaded under this name first, then server-side `rename`d to
# REMOTE_FILE only once the transfer has fully succeeded -- so a
# connection drop mid-upload can never leave a truncated file sitting
# under the same name a real, valid backup would use. See the upload
# step below.
REMOTE_PARTIAL="${REMOTE_FILE}.partial"

cleanup() {
  rm -rf "$WORKDIR"
  # DRY_RUN keeps the finished archive on disk for inspection instead of
  # deleting it here -- its path is printed explicitly near the end of
  # the script either way, so this is the ONLY place that decides
  # whether it survives, never an accident of forgetting to clean up.
  if [ "$DRY_RUN" != "1" ]; then
    rm -f "$TMP_TAR"
  fi
}
trap cleanup EXIT

if [ "$DRY_RUN" = "1" ]; then
  echo "DRY_RUN=1: skipping ${CONFIG_FILE} entirely -- no SFTP connection will be opened (no remote listing, no upload, no prune)."
  BAK_HOST="(dry-run, not used)"
  BAK_PATH="(dry-run, not used)"
else
  if [ ! -f "$CONFIG_FILE" ]; then
    echo "Error: credential file $CONFIG_FILE not found." >&2
    echo "Create it with BAK_HOST, BAK_USER, BAK_PORT, BAK_PATH, BAK_PASS." >&2
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

  if ! command -v sshpass >/dev/null 2>&1; then
    echo "Error: sshpass is not installed. Install with: sudo apt install sshpass" >&2
    exit 1
  fi
fi
if ! command -v pg_dump >/dev/null 2>&1; then
  echo "Error: pg_dump is not installed (postgresql-client)." >&2
  exit 1
fi

ENV_FILE="$PROJECT_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "Error: $ENV_FILE not found -- can't read DB credentials." >&2
  exit 1
fi

# Needed for the Runtime Foundation E7B recovery-payload validation step
# below (manage.py validate_runtime_recovery_payload) -- checked here,
# early, alongside this script's other prerequisite checks, rather than
# failing deep into the run after the database dump/app archive work.
APP_VENV_PYTHON="$PROJECT_DIR/venv/bin/python"
if [ ! -x "$APP_VENV_PYTHON" ]; then
  echo "Error: $APP_VENV_PYTHON not found or not executable -- needed to validate the Runtime Foundation E7 recovery payload." >&2
  exit 1
fi

# Runs an sftp batch script (read from stdin), retrying on failure --
# a real connection-reset was observed on this remote host when opening
# a second session immediately after a first one, so this isn't just
# theoretical hardening.
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

mkdir -p "$WORKDIR"

echo "IsadoraAir backup starting (script v${SCRIPT_VERSION})..."
echo "  Host:   ${BAK_HOST}"
echo "  Remote: ${BAK_PATH}"
echo

TO_DELETE=()
if [ "$DRY_RUN" = "1" ]; then
  echo "DRY_RUN=1: skipping remote retention listing."
  echo
else
  # Listed up front, before the slower dump/tar work below, so this
  # connection is naturally spaced apart from the combined upload+prune
  # session at the end rather than opening two sftp sessions back-to-back.
  echo "Checking for backups older than ${RETENTION_DAYS} days to prune..."
  CUTOFF=$(date -d "${RETENTION_DAYS} days ago" +%Y%m%d)
  REMOTE_LISTING=$(sftp_run <<EOF
cd ${BAK_PATH}
ls -1 isadoraair-backup-*.tar.gz
bye
EOF
  )

  while IFS= read -r name; do
    # Only match our own naming pattern -- anything else on the remote
    # (there's no reason there would be, but this is a delete path) is
    # left alone rather than guessed at. Also never matches our own
    # *.partial staging name, so a leftover partial from a prior failed
    # run is never silently deleted here -- see the upload step for why
    # that's intentional (a human should notice and investigate it).
    if [[ "$name" =~ ^isadoraair-backup-([0-9]{8})-[0-9]{6}\.tar\.gz$ ]]; then
      file_date="${BASH_REMATCH[1]}"
      if [ "$file_date" -lt "$CUTOFF" ]; then
        TO_DELETE+=("$name")
      fi
    fi
  done <<< "$REMOTE_LISTING"

  if [ "${#TO_DELETE[@]}" -eq 0 ]; then
    echo "  Nothing older than ${RETENTION_DAYS} days."
  else
    echo "  Will prune ${#TO_DELETE[@]} backup(s):"
    printf '    %s\n' "${TO_DELETE[@]}"
  fi
  echo
fi

echo "Dumping PostgreSQL database..."
# Same DB_* values Django itself reads from .env via python-decouple --
# not duplicated in the cred file, so there's one source of truth for
# DB credentials.
DB_NAME=$(grep -E '^DB_NAME=' "$ENV_FILE" | head -1 | cut -d= -f2-)
DB_USER=$(grep -E '^DB_USER=' "$ENV_FILE" | head -1 | cut -d= -f2-)
DB_PASSWORD=$(grep -E '^DB_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2-)
DB_HOST=$(grep -E '^DB_HOST=' "$ENV_FILE" | head -1 | cut -d= -f2-)
DB_PORT=$(grep -E '^DB_PORT=' "$ENV_FILE" | head -1 | cut -d= -f2-)
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
: "${DB_NAME:?DB_NAME not found in $ENV_FILE}"
: "${DB_USER:?DB_USER not found in $ENV_FILE}"

PGPASSWORD="$DB_PASSWORD" pg_dump -Fc \
  -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME" \
  -f "$WORKDIR/database.dump"
# Belt-and-suspenders beyond `set -e`/pg_dump's own exit code: an empty
# or missing dump file must never silently pass as "the backup ran ok".
if [ ! -s "$WORKDIR/database.dump" ]; then
  echo "Error: database dump is empty or missing -- aborting before upload." >&2
  exit 1
fi
echo "  $(du -h "$WORKDIR/database.dump" | cut -f1) database dump"

echo "Archiving application tree (code, .env, media)..."
# -h/--dereference: PROJECT_DIR (/opt/isadoraair by convention) is itself
# a symlink to the real checkout. Without -h, tar does NOT follow a
# symlink passed as its own named argument -- it archives the symlink
# record itself (one tiny entry) and stops there, silently producing a
# ~4KB "app archive" containing no code, no .env, no media/. That's a
# successful exit code with `set -e`/`set -o pipefail` powerless to catch
# it, since tar itself never errors -- it just archived the wrong thing.
# Caught by the 2026-08-12 DRY_RUN validation pass; see
# isadoraair/tests/test_deploy_backup_script.py's symlink regression test.
tar -h -czf "$WORKDIR/app.tar.gz" \
  --exclude=".git" \
  --exclude="venv" \
  --exclude="__pycache__" \
  --exclude="*.pyc" \
  --exclude="staticfiles" \
  --exclude="$(basename "$PROJECT_DIR")/media/album_art_cache" \
  --exclude="$(basename "$PROJECT_DIR")/.env.bak" \
  --exclude="$(basename "$PROJECT_DIR")/.env.lock" \
  -C "$(dirname "$PROJECT_DIR")" "$(basename "$PROJECT_DIR")"
echo "  $(du -h "$WORKDIR/app.tar.gz" | cut -f1) app archive"

# Belt-and-suspenders beyond the -h flag above: confirm the archive we
# just built actually contains the application, not just a redigested
# symlink stub. manage.py is the single most load-bearing file possible
# to check for (its absence means literally nothing else here would
# work), .env is the specific thing a bare symlink-stub archive would
# have skipped that no other component of this backup captures elsewhere.
#
# Captured to a variable rather than piped straight into `grep -q`: under
# `set -o pipefail`, `grep -q` exiting the instant it finds a match sends
# tar a SIGPIPE on its next write, and pipefail reports THAT nonzero exit
# for the whole pipeline even though grep matched successfully -- a false
# abort, caught by this script's own DRY_RUN validation immediately after
# the -h fix above was first written.
APP_TAR_LISTING=$(tar -tzf "$WORKDIR/app.tar.gz")
if ! grep -qE "^$(basename "$PROJECT_DIR")/manage\.py$" <<< "$APP_TAR_LISTING"; then
  echo "Error: app.tar.gz is missing manage.py -- application tree was not captured. Aborting before upload." >&2
  exit 1
fi
if ! grep -qE "^$(basename "$PROJECT_DIR")/\.env$" <<< "$APP_TAR_LISTING"; then
  echo "Error: app.tar.gz is missing .env -- application tree was not captured. Aborting before upload." >&2
  exit 1
fi
# Mirror check for the 2026-08-12 follow-up exclusions: .env itself must
# be in, but its stray .env.bak/.env.lock siblings must stay out (the
# latter briefly leaked into every backup before the --exclude flags
# above were added).
if grep -qE "^$(basename "$PROJECT_DIR")/\.env\.(bak|lock)$" <<< "$APP_TAR_LISTING"; then
  echo "Error: app.tar.gz contains .env.bak or .env.lock -- exclude flags did not take effect. Aborting before upload." >&2
  exit 1
fi

echo "Copying live nginx/systemd configs..."
mkdir -p "$WORKDIR/etc-live"
# sites-enabled/isadoraair MUST be a symlink to sites-available/isadoraair
# (see deploy/README.md's "One authoritative nginx config") -- that's
# exactly why sites-available is the correct, sufficient file to back up.
# If this ever becomes a plain file diverging from sites-enabled again,
# that split-brain is the bug to fix (on the host), not something to work
# around here.
[ -r /etc/nginx/sites-available/isadoraair ] && \
  cp /etc/nginx/sites-available/isadoraair "$WORKDIR/etc-live/isadoraair.nginx"
[ -r /etc/nginx/snippets/isadoraair-locations.conf ] && \
  cp /etc/nginx/snippets/isadoraair-locations.conf "$WORKDIR/etc-live/isadoraair-locations.conf"
for svc in isadoraair-gunicorn isadoraair-engine isadoraair-encoders isadoraair-monitoring isadoraair-rbds stereotool; do
  unit="/etc/systemd/system/${svc}.service"
  [ -r "$unit" ] && cp "$unit" "$WORKDIR/etc-live/"
done

echo "Copying StereoTool processing profile (.sts), if present..."
mkdir -p "$WORKDIR/stereotool"
STS_COUNT=0
if [ -d "$STEREOTOOL_DIR" ]; then
  while IFS= read -r -d '' sts; do
    cp -p "$sts" "$WORKDIR/stereotool/"
    STS_COUNT=$((STS_COUNT + 1))
  done < <(find "$STEREOTOOL_DIR" -maxdepth 1 -iname '*.sts' -print0)
fi
echo "  ${STS_COUNT} .sts profile(s)"

echo "Copying small operator-created station content (FX carts, voicetracks)..."
mkdir -p "$WORKDIR/srv-content"
for sub in carts voicetracks; do
  src="/srv/isadoraair/$sub"
  if [ -d "$src" ]; then
    cp -a "$src" "$WORKDIR/srv-content/$sub"
  fi
done
echo "  $(du -sh "$WORKDIR/srv-content" 2>/dev/null | cut -f1) of station content"

echo "Copying royalty/SoundExchange report filings, if present..."
REPORTS_DIR=$(grep -E '^REPORTS_ROOT=' "$ENV_FILE" | head -1 | cut -d= -f2-)
REPORTS_DIR="${REPORTS_DIR:-/var/lib/isadoraair/reports}"
if [ -d "$REPORTS_DIR" ]; then
  cp -a "$REPORTS_DIR" "$WORKDIR/reports"
  echo "  $(du -sh "$WORKDIR/reports" 2>/dev/null | cut -f1) of report filings"
else
  echo "  (none found at $REPORTS_DIR)"
fi

echo "Validating and including the current Runtime Foundation E7 recovery payload..."
# Small, safe extraction of one field from validate_runtime_recovery_payload's
# --json output -- pure stdlib json, no eval, never passed anything but
# this script's own already-validated subprocess output.
recovery_json_get() {
  python3 -c "
import json, sys
data = json.loads(sys.argv[1])
for key in sys.argv[2].split('.'):
    data = (data or {}).get(key) if isinstance(data, dict) else None
if isinstance(data, list):
    print(','.join(str(x) for x in data))
elif data is None:
    print('')
else:
    print(data)
" "$1" "$2"
}

set +e
RECOVERY_PAYLOAD_STATUS_JSON=$("$APP_VENV_PYTHON" "$PROJECT_DIR/manage.py" \
  validate_runtime_recovery_payload --base-root "$RECOVERY_PAYLOAD_ROOT" --current --json \
  --require-components "$BACKUP_REQUIRED_RECOVERY_COMPONENTS")
RECOVERY_PAYLOAD_EXIT=$?
set -e

RECOVERY_PAYLOAD_INCLUDED=0
if [ "$RECOVERY_PAYLOAD_EXIT" -eq 2 ] && [ -z "$BACKUP_REQUIRED_RECOVERY_COMPONENTS" ]; then
  # Exit 2 = RecoveryPayloadNotConfiguredError specifically (never any
  # other failure) -- Runtime Foundation E7B simply hasn't been adopted
  # on this host yet, and no operator policy says it must be. Same,
  # unchanged backup behavior as before Runtime Foundation E7 existed.
  echo "  no current recovery payload is configured at ${RECOVERY_PAYLOAD_ROOT} -- Runtime Foundation E7B not yet adopted on this host. Continuing without runtime-recovery/ in this archive."
  RECOVERY_PAYLOAD_MANIFEST_BLOCK="Runtime recovery payload: not included (no current payload configured at ${RECOVERY_PAYLOAD_ROOT})"
elif [ "$RECOVERY_PAYLOAD_EXIT" -ne 0 ]; then
  # Every other failure is fatal, unconditionally -- exit 2 with a
  # required-components policy configured (the payload MUST exist),
  # exit 1 (found but invalid/tampered/stale/wrong-digest), or exit 1
  # for a configured-but-unsafe base_root/current pointer. A configured-
  # but-broken payload is never treated as "not yet adopted."
  echo "Error: Runtime Foundation E7 recovery payload validation failed -- aborting before upload rather than producing a backup that appears to carry a trustworthy runtime recovery payload when it does not." >&2
  echo "$RECOVERY_PAYLOAD_STATUS_JSON" >&2
  exit 1
else
  RECOVERY_PAYLOAD_RESOLVED_PATH=$(recovery_json_get "$RECOVERY_PAYLOAD_STATUS_JSON" resolved_path)
  RECOVERY_PAYLOAD_ID=$(recovery_json_get "$RECOVERY_PAYLOAD_STATUS_JSON" payload_id)
  RECOVERY_PAYLOAD_SCHEMA_VERSION=$(recovery_json_get "$RECOVERY_PAYLOAD_STATUS_JSON" schema_version)
  RECOVERY_PAYLOAD_PRODUCT_DIGEST=$(recovery_json_get "$RECOVERY_PAYLOAD_STATUS_JSON" product_contract_sha256)
  RECOVERY_PAYLOAD_TTS_STATE=$(recovery_json_get "$RECOVERY_PAYLOAD_STATUS_JSON" components.tts.state)
  RECOVERY_PAYLOAD_NATIVE_STATE=$(recovery_json_get "$RECOVERY_PAYLOAD_STATUS_JSON" components.native_fdkaac.state)
  RECOVERY_PAYLOAD_TTS_COMPONENTS=$(recovery_json_get "$RECOVERY_PAYLOAD_STATUS_JSON" tts_components)
  RECOVERY_PAYLOAD_PIPER_FRESHNESS=$(recovery_json_get "$RECOVERY_PAYLOAD_STATUS_JSON" piper_freshness.state)
  RECOVERY_PAYLOAD_POLICY_REQUIRED=$(recovery_json_get "$RECOVERY_PAYLOAD_STATUS_JSON" policy.required)
  RECOVERY_PAYLOAD_POLICY_SATISFIED=$(recovery_json_get "$RECOVERY_PAYLOAD_STATUS_JSON" policy.satisfied)

  mkdir -p "$WORKDIR/runtime-recovery"
  # The backup unit runs as the application account. The trusted source
  # remains administrator-owned 0755/0644 and is readable but not
  # writable. Do not attempt to preserve root ownership in the caller's
  # workdir; byte identity is re-proven on the copy immediately below.
  cp -R "$RECOVERY_PAYLOAD_RESOLVED_PATH/." "$WORKDIR/runtime-recovery/"

  # Belt-and-suspenders, matching the app.tar.gz manage.py/.env checks
  # above: re-validate the COPY actually inside the working directory
  # (a direct payload-path call, not --current) before trusting it goes
  # into the final archive -- proves the copy itself round-tripped
  # correctly, never just the original.
  if ! "$APP_VENV_PYTHON" "$PROJECT_DIR/manage.py" validate_runtime_recovery_payload "$WORKDIR/runtime-recovery" >/dev/null; then
    echo "Error: the copied runtime-recovery payload inside the backup working directory failed re-validation -- aborting before upload." >&2
    exit 1
  fi

  echo "  included: payload ${RECOVERY_PAYLOAD_ID} (tts=${RECOVERY_PAYLOAD_TTS_STATE} [${RECOVERY_PAYLOAD_TTS_COMPONENTS}], native_fdkaac=${RECOVERY_PAYLOAD_NATIVE_STATE})"
  RECOVERY_PAYLOAD_INCLUDED=1
  RECOVERY_PAYLOAD_MANIFEST_BLOCK="Runtime recovery payload: included
Runtime recovery payload ID: ${RECOVERY_PAYLOAD_ID}
Runtime recovery payload schema version: ${RECOVERY_PAYLOAD_SCHEMA_VERSION}
Runtime recovery product-contract digest: ${RECOVERY_PAYLOAD_PRODUCT_DIGEST}
Runtime recovery tts component: ${RECOVERY_PAYLOAD_TTS_STATE} (${RECOVERY_PAYLOAD_TTS_COMPONENTS})
Runtime recovery native fdkaac component: ${RECOVERY_PAYLOAD_NATIVE_STATE}
Runtime recovery Piper station-selection freshness: ${RECOVERY_PAYLOAD_PIPER_FRESHNESS}
Runtime recovery required-component policy: ${RECOVERY_PAYLOAD_POLICY_REQUIRED:-(none configured)}
Runtime recovery required-component policy satisfied: ${RECOVERY_PAYLOAD_POLICY_SATISFIED:-n/a}"
fi

# Only a payload satisfying a NON-EMPTY explicit policy earns archive
# format 3.0.0 / self_contained_v3. Before station adoption, this E7B-
# capable script keeps producing a clearly labelled legacy 2.1.0
# archive, preserving nightly-backup availability without overstating DR.
RECOVERY_ARCHIVE_METADATA_JSON=$(python3 "$SCRIPT_DIR/restore/runtime_recovery_archive.py" \
  write-metadata \
  --status-json "$RECOVERY_PAYLOAD_STATUS_JSON" \
  --script-version "$SCRIPT_VERSION" \
  --output "$WORKDIR/runtime-recovery-archive.json")
RECOVERY_ARCHIVE_FORMAT=$(recovery_json_get "$RECOVERY_ARCHIVE_METADATA_JSON" archive_format_version)
RECOVERY_ARCHIVE_CLASS=$(recovery_json_get "$RECOVERY_ARCHIVE_METADATA_JSON" recovery_class)
echo

echo "Encrypting recovery credentials (if configured)..."
# See deploy/encrypt_recovery_credentials.sh's own header for the full
# security model and failure policy. Run unconditionally (including under
# DRY_RUN) -- DRY_RUN's own contract is "no SFTP connection", not "skip
# every filesystem operation"; exercising this step locally under DRY_RUN
# is exactly how an operator proves the mechanism works before trusting it
# on a real nightly run. This step never touches ~/.iasboxbu.cred's SFTP
# credentials (BAK_HOST etc.) -- it only ever reads the file's raw bytes
# as an opaque blob to encrypt, the same as the other two credential
# files, never parsing/using any value from it.
RECOVERY_CRED_DIR="$WORKDIR/recovery-credentials"
RECOVERY_CRED_STATUS_OUTPUT=""
if ! RECOVERY_CRED_STATUS_OUTPUT=$("$SCRIPT_DIR/encrypt_recovery_credentials.sh" "$RECOVERY_CRED_DIR"); then
  echo "Error: encrypted recovery-credential preservation is configured but failed -- see output above. Aborting before upload rather than producing a backup that appears to protect recovery credentials when it does not." >&2
  echo "$RECOVERY_CRED_STATUS_OUTPUT" >&2
  exit 1
fi
RECOVERY_CRED_ENABLED=0
RECOVERY_CRED_LINES=""
while IFS= read -r line; do
  case "$line" in
    STATUS=enabled) RECOVERY_CRED_ENABLED=1 ;;
    STATUS=disabled) RECOVERY_CRED_ENABLED=0 ;;
    "CRED "*) RECOVERY_CRED_LINES="${RECOVERY_CRED_LINES}${line}"$'\n' ;;
  esac
done <<< "$RECOVERY_CRED_STATUS_OUTPUT"
if [ "$RECOVERY_CRED_ENABLED" -eq 1 ]; then
  echo "  enabled -- see manifest for per-file inclusion status"
  RECOVERY_CRED_MANIFEST_BLOCK="Recovery credential encryption: enabled
Recovery credential cipher: age"
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    # "CRED iasboxbu included" -> "Recovery credential iasboxbu: included"
    name="${line#CRED }"; name="${name% *}"
    status="${line##* }"
    RECOVERY_CRED_MANIFEST_BLOCK="${RECOVERY_CRED_MANIFEST_BLOCK}
Recovery credential ${name}: ${status}"
  done <<< "$RECOVERY_CRED_LINES"
else
  echo "  disabled (BACKUP_RECOVERY_AGE_RECIPIENT/_FILE not configured) -- ~/.iasboxbu.cred, ~/.syndicated_ingest.cred, ~/.ogremote_ingest.cred are NOT included in this archive, same as every backup before this feature existed"
  RECOVERY_CRED_MANIFEST_BLOCK="Recovery credential encryption: disabled (BACKUP_RECOVERY_AGE_RECIPIENT/_FILE not configured)"
fi
echo

echo "Writing backup manifest..."
GIT_SHA="unknown"
if command -v git >/dev/null 2>&1 && [ -d "$PROJECT_DIR/.git" ]; then
  GIT_SHA=$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
fi
cat > "$WORKDIR/MANIFEST.txt" <<MANIFEST
IsadoraAir disaster-recovery backup manifest
=============================================
Backup script version: ${SCRIPT_VERSION}
Archive format version: ${RECOVERY_ARCHIVE_FORMAT}
Runtime recovery archive class: ${RECOVERY_ARCHIVE_CLASS}
Created (UTC):          $(date -u -Iseconds)
IsadoraAir Git SHA:     ${GIT_SHA}
Database name:          ${DB_NAME}

Contents of this archive:
  database.dump                          pg_dump -Fc of the database
  app.tar.gz                             application tree (code, .env, media/)
  etc-live/isadoraair.nginx              live nginx site config (sites-available/isadoraair)
  etc-live/isadoraair-locations.conf     live shared nginx location-block snippet
  etc-live/isadoraair-*.service          live systemd units, 5 core services
  etc-live/stereotool.service            live StereoTool supervision unit (if present)
  stereotool/*.sts                       StereoTool processing profile(s) (${STS_COUNT} found)
  srv-content/carts/                     FX Cart audio (operator-uploaded)
  srv-content/voicetracks/               recorded voicetrack audio (operator-created)
  reports/                               SoundExchange/royalty report filings (if present)
  runtime-recovery/                      Runtime Foundation E7 disaster-recovery payload (if configured -- see below)
  runtime-recovery-archive.json          machine-readable archive/recovery classification
  recovery-credentials/*.age             age-encrypted companion credential copies (if configured -- see below; NEVER plaintext)

Deliberately EXCLUDED from this backup (see docs/DISASTER_RECOVERY.md):
  /srv/isadoraair/music        717+ GB audio library -- separate storage-resilience task
  /srv/isadoraair/waveforms    regenerable via manage.py analyze_tracks
  /srv/isadoraair/aircheck     high-growth, own retention policy
  /srv/isadoraair/rip_staging  transient working directory
  /srv/isadoraair/mitd_artbell 44+ GB syndicated-show staging, own future sizing decision
  .git/, venv/, __pycache__/, staticfiles/, media/album_art_cache/
  .env.bak, .env.lock                    stray local files, not restore-relevant (.env itself IS included)

${RECOVERY_PAYLOAD_MANIFEST_BLOCK}
NOTE: an included runtime recovery payload is the actual Runtime
Foundation E3/E4 offline material (wheels, models, native source
archives) needed to restore Kokoro/Piper/fdkaac without network access
-- see docs/RUNTIME_BACKUP_PAYLOAD.md. It was NOT built or acquired by
this backup run; it is a copy of whatever an operator already prepared
and activated on this host via manage.py prepare_runtime_recovery_payload.
Only archive format 3.0.0 / class self_contained_v3 is self-contained
for Runtime Foundation E restore. An E7B-capable script run without a
policy-satisfying payload writes archive format 2.1.0 / class
legacy_non_self_contained, even if an optional payload happened to be
copied; the script implementation version never doubles as the archive
class.

${RECOVERY_CRED_MANIFEST_BLOCK}
NOTE: an "included" recovery credential above is ciphertext only, decryptable
solely with the age PRIVATE key held externally (never on this host) --
see docs/DISASTER_RECOVERY_RESTORE.md "Encrypted recovery-credential
preservation". It is NOT a bootstrap source for retrieving this archive
in the first place -- ~/.iasboxbu.cred's own external off-host copy is
still what a recovery starts from; this encrypted copy is for keeping
that (and the other two) current/authoritative going forward and for
restoring them once the archive is already in hand.

Secrets NEVER included in ANY form, even encrypted -- must be
reprovisioned from elsewhere on restore (see docs/DISASTER_RECOVERY.md
"Secret reprovisioning boundary"):
  acme.sh / Let's Encrypt DNS-01 provider credentials
  StereoTool license (not a Phase 5 blocker -- see docs/DISASTER_RECOVERY_RESTORE.md; StereoTool runs unlicensed with only an occasional watermark until manually relicensed post-restore)
MANIFEST

echo "Building final archive..."
tar czf "$TMP_TAR" -C "$WORKDIR" .
echo "Archive created:"
ls -lh "$TMP_TAR"
echo

echo "Verifying archive integrity before upload..."
if ! tar -tzf "$TMP_TAR" > /dev/null; then
  echo "Error: freshly-built archive failed its own integrity check -- aborting before upload." >&2
  exit 1
fi
echo "  ok"
echo

if [ "$DRY_RUN" = "1" ]; then
  echo "DRY_RUN=1: not uploading, not pruning. Archive left in place for inspection:"
  echo "  ${TMP_TAR}"
  echo
  echo "IsadoraAir backup dry run completed successfully (local archive only, nothing touched remotely)."
else
  echo "Uploading via SFTP (staged, then promoted atomically; pruning old backups in the same session)..."
  {
    echo "cd ${BAK_PATH}"
    echo "put ${TMP_TAR} ${REMOTE_PARTIAL}"
    # Only reachable if the put above fully succeeded (sftp batch mode
    # stops on the first failed command) -- so a connection drop mid-
    # upload can never leave a file sitting under the FINAL name that a
    # later listing/restore would mistake for a complete, valid backup.
    # The rename itself is a fast server-side metadata operation, not a
    # second data transfer, so it isn't a second place this can partially
    # fail in a way that matters.
    echo "rename ${REMOTE_PARTIAL} ${REMOTE_FILE}"
    for name in "${TO_DELETE[@]}"; do
      echo "rm ${name}"
    done
    echo "bye"
  } | sftp_run

  echo "Upload complete: ${REMOTE_FILE}"
  if [ "${#TO_DELETE[@]}" -gt 0 ]; then
    echo "Pruned ${#TO_DELETE[@]} backup(s) older than ${RETENTION_DAYS} days."
  fi
  echo
  echo "IsadoraAir backup completed successfully."
fi
