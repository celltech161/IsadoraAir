#!/usr/bin/env bash
# deploy/encrypt_recovery_credentials.sh -- IsadoraAir 1.2 disaster-recovery
# Phase 4.5 final follow-up (2026-08-18).
#
# Encrypts the three companion-project credential files (never IsadoraAir's
# own .env, which is already covered by the ordinary app.tar.gz backup) into
# age ciphertext, for inclusion in the nightly backup archive by
# deploy/backup_isadoraair.sh. Split out as its own small, standalone,
# independently-testable script -- same architectural pattern this repo
# already uses for deploy/restore/inspect_backup.sh (a focused helper with
# minimal dependencies, callable both from the real backup flow and from a
# test suite using disposable synthetic input, never requiring the heavy
# stuff -- pg_dump/SFTP -- the main backup script needs).
#
# SECURITY MODEL: this script only ever uses a PUBLIC age recipient to
# encrypt. It has no code path that reads, generates, or stores a private
# age identity/decryption key -- that key lives only off-host, per the
# operator's own recovery-key custody plan (see
# docs/DISASTER_RECOVERY_RESTORE.md's "Encrypted recovery-credential
# preservation" section). Compromising this script, the host it runs on, or
# any backup archive it produces does NOT grant decryption capability.
#
# Configuration (both non-secret -- the recipient is a PUBLIC key):
#   BACKUP_RECOVERY_AGE_RECIPIENT       Direct age recipient string
#                                       (age1...), OR
#   BACKUP_RECOVERY_AGE_RECIPIENT_FILE  Path to a file containing just the
#                                       recipient (first non-comment,
#                                       non-blank line) -- preferred for a
#                                       real install, since it keeps the
#                                       systemd unit/environment free of a
#                                       long inline string.
# Neither set => feature is DISABLED. This is the deliberate default for
# every fresh/generic install -- see this script's own "disabled" branch
# below. A fresh install must never fail to back up solely because
# encrypted credential preservation was never configured.
#
# Once EITHER is set, the feature is explicitly configured and this script
# fails closed (nonzero exit, no partial/misleading output files) on any of:
#   - the `age` binary not being installed,
#   - BACKUP_RECOVERY_AGE_RECIPIENT_FILE configured but unreadable/empty,
#   - the resolved recipient failing a basic structural sanity check,
#   - any individual `age` encryption invocation failing,
#   - a resulting .age file being empty.
# An individual SOURCE credential file simply not existing on this host is
# NOT a failure -- none of the three are required for IsadoraAir's own core
# function (see docs/RUNTIME_BASELINE.md's restore-order map: these are
# companion-project extras), so a station not running one of those
# companion projects correctly has no file to encrypt for it.
#
# Source credential paths are overridable via env for testability (never
# for production use -- production always uses the real $HOME paths):
#   IASBOXBU_CRED_FILE            default $HOME/.iasboxbu.cred
#   SYNDICATED_INGEST_CRED_FILE   default $HOME/.syndicated_ingest.cred
#   OGREMOTE_INGEST_CRED_FILE     default $HOME/.ogremote_ingest.cred
#
# Usage:
#   deploy/encrypt_recovery_credentials.sh <output-dir>
#
# Writes <output-dir>/<name>.cred.age for each credential file that exists,
# never a plaintext sibling. <output-dir> is created mode 0700 if it
# doesn't exist; each .age file is chmod 0600 after writing (age's own
# default is already restrictive, but this doesn't rely on that or on
# umask -- explicit, not assumed).
#
# Prints simple key/value status lines to stdout (never a secret value) --
# deploy/backup_isadoraair.sh parses these directly into MANIFEST.txt so
# there is exactly one place that decides what "included"/"absent" means,
# not two copies that could drift:
#   STATUS=disabled
# or
#   STATUS=enabled
#   CIPHER=age
#   CRED iasboxbu included
#   CRED syndicated_ingest absent
#   CRED ogremote_ingest included
#
# Exit codes: 0 = success (whether disabled, or enabled and fully
# succeeded). 1 = explicitly configured but failed closed -- caller must
# abort before upload; see this script's own error messages on stderr for
# which precondition failed.
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: encrypt_recovery_credentials.sh <output-dir>" >&2
  exit 2
fi
OUTPUT_DIR="$1"

BACKUP_RECOVERY_AGE_RECIPIENT="${BACKUP_RECOVERY_AGE_RECIPIENT:-}"
BACKUP_RECOVERY_AGE_RECIPIENT_FILE="${BACKUP_RECOVERY_AGE_RECIPIENT_FILE:-}"

IASBOXBU_CRED_FILE="${IASBOXBU_CRED_FILE:-$HOME/.iasboxbu.cred}"
SYNDICATED_INGEST_CRED_FILE="${SYNDICATED_INGEST_CRED_FILE:-$HOME/.syndicated_ingest.cred}"
OGREMOTE_INGEST_CRED_FILE="${OGREMOTE_INGEST_CRED_FILE:-$HOME/.ogremote_ingest.cred}"

# ---- Not configured at all -> feature OFF, not an error -------------------
# Deliberately checked before anything else (no `age` lookup, no file I/O
# beyond this) -- a fresh generic install with neither var set must see
# zero difference from before this feature existed.
if [ -z "$BACKUP_RECOVERY_AGE_RECIPIENT" ] && [ -z "$BACKUP_RECOVERY_AGE_RECIPIENT_FILE" ]; then
  echo "STATUS=disabled"
  exit 0
fi

# ---- Explicitly configured from here on -- fail closed on any problem -----
if [ -z "$BACKUP_RECOVERY_AGE_RECIPIENT" ]; then
  if [ ! -f "$BACKUP_RECOVERY_AGE_RECIPIENT_FILE" ]; then
    echo "Error: BACKUP_RECOVERY_AGE_RECIPIENT_FILE=$BACKUP_RECOVERY_AGE_RECIPIENT_FILE does not exist." >&2
    exit 1
  fi
  # First non-blank, non-comment line -- lets the file carry a leading
  # explanatory comment (e.g. "# IsadoraAir backup recovery recipient")
  # without operator error.
  BACKUP_RECOVERY_AGE_RECIPIENT=$(grep -vE '^\s*(#|$)' "$BACKUP_RECOVERY_AGE_RECIPIENT_FILE" | head -1 || true)
  if [ -z "$BACKUP_RECOVERY_AGE_RECIPIENT" ]; then
    echo "Error: BACKUP_RECOVERY_AGE_RECIPIENT_FILE=$BACKUP_RECOVERY_AGE_RECIPIENT_FILE exists but contains no usable recipient line." >&2
    exit 1
  fi
fi

# Structural sanity check only, not a full validator -- age recipients are
# bech32 "age1" + lowercase alphanumeric, normally 62 characters total. The
# REAL validity check is the actual `age -r` invocation below succeeding;
# this just turns an obviously-wrong value (typo, wrong file, accidentally
# pasted a private key by mistake) into one clear error up front instead of
# three confusing per-file encryption failures.
if ! [[ "$BACKUP_RECOVERY_AGE_RECIPIENT" =~ ^age1[a-z0-9]{20,}$ ]]; then
  echo "Error: configured recipient does not look like a valid age public recipient (expected age1... , lowercase alphanumeric). Refusing to guess -- check BACKUP_RECOVERY_AGE_RECIPIENT/_FILE. Never configure a private key (AGE-SECRET-KEY-...) here -- that must never exist on this host." >&2
  exit 1
fi
# Extra-explicit guard: a private identity accidentally pasted into the
# recipient slot must never silently "work" -- age's own -r flag would
# reject an AGE-SECRET-KEY-... string anyway (wrong prefix, fails the regex
# above already), but this makes the intent unmistakable to a future reader
# rather than relying solely on the prefix mismatch to save us.
case "$BACKUP_RECOVERY_AGE_RECIPIENT" in
  AGE-SECRET-KEY-*)
    echo "Error: BACKUP_RECOVERY_AGE_RECIPIENT looks like a PRIVATE age identity, not a public recipient. A private key must never be configured on this host. Aborting." >&2
    exit 1
    ;;
esac

if ! command -v age >/dev/null 2>&1; then
  echo "Error: recovery-credential encryption is configured but the 'age' binary is not installed. Install with: sudo apt install age" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
chmod 700 "$OUTPUT_DIR"

echo "STATUS=enabled"
echo "CIPHER=age"

# name:path pairs -- deliberately NOT including IsadoraAir's own .env here;
# that's already fully covered by the ordinary app.tar.gz backup and has a
# completely different restore path (20-application.sh), not this one.
CRED_ENTRIES=(
  "iasboxbu:${IASBOXBU_CRED_FILE}"
  "syndicated_ingest:${SYNDICATED_INGEST_CRED_FILE}"
  "ogremote_ingest:${OGREMOTE_INGEST_CRED_FILE}"
)

for entry in "${CRED_ENTRIES[@]}"; do
  name="${entry%%:*}"
  src="${entry#*:}"
  if [ ! -f "$src" ]; then
    echo "CRED ${name} absent"
    continue
  fi
  dest="$OUTPUT_DIR/${name}.cred.age"
  # age reads the plaintext source directly and writes ciphertext directly
  # -- no intermediate plaintext copy, no shell variable ever holds the
  # file's contents. -r takes the PUBLIC recipient only; there is no -i/
  # --identity anywhere in this script, on purpose (a private key has no
  # legitimate use in the encrypt direction).
  if ! age -r "$BACKUP_RECOVERY_AGE_RECIPIENT" -o "$dest" "$src"; then
    echo "Error: age encryption failed for ${name} (source: $src)." >&2
    exit 1
  fi
  if [ ! -s "$dest" ]; then
    echo "Error: age produced an empty ciphertext file for ${name} -- treating as failure, not a valid backup." >&2
    exit 1
  fi
  chmod 600 "$dest"
  echo "CRED ${name} included"
done
