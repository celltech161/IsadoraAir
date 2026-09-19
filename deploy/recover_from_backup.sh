#!/usr/bin/env bash
# Archive-only bare-metal recovery bootstrap.
#
# Operator contract:
#   1. Install a fresh supported Ubuntu release.
#   2. Copy one accepted IsadoraAir backup archive onto the machine.
#   3. Run this script as the intended IsadoraAir operator account:
#        recover_from_backup.sh /tmp/isadoraair-backup-YYYYMMDD-HHMMSS.tar.gz
#
# The backup itself carries recovery/IsadoraAir.bundle plus the two private
# companion repository bundles. Public OS/Python dependencies are installed
# from normal online sources. No writable recovery USB, frozen E8 closure,
# GitHub SSH key, or separately-packaged recovery-media root is required.
#
# This file is intentionally only a bootstrap. The actual restore remains the
# versioned deploy/restore machinery from the exact IsadoraAir commit embedded
# in the supplied backup.
set -euo pipefail

usage() {
  echo "Usage: $0 /path/to/isadoraair-backup-*.tar.gz" >&2
}

fail() {
  echo "ERROR: $*" >&2
  return 1
}

if [ "$#" -ne 1 ]; then
  usage
  exit 2
fi

ARCHIVE="$(readlink -f "$1")"
ISA_USER="$(id -un)"
ISA_GROUP="$(id -gn)"

if [ "$(id -u)" -eq 0 ]; then
  fail "Do NOT run archive recovery with sudo/root. Run it as the intended IsadoraAir operator account."
  exit 1
fi
if [ ! -f "$ARCHIVE" ]; then
  fail "backup archive not found: $ARCHIVE"
  exit 1
fi
for cmd in tar git python3 sha256sum sudo; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    fail "required command is missing on the fresh host: $cmd"
    exit 1
  fi
done

ARCHIVE_SHA256="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
STATE_ROOT="$HOME/.local/state/isadoraair/archive-recovery/$ARCHIVE_SHA256"
RECOVERY_ROOT="$STATE_ROOT/recovery"

mkdir -p "$STATE_ROOT"
chmod 700 "$HOME/.local/state/isadoraair/archive-recovery" "$STATE_ROOT" 2>/dev/null || true

echo "IsadoraAir Archive Recovery"
echo "==========================="
echo "Archive:  $ARCHIVE"
echo "SHA256:   $ARCHIVE_SHA256"
echo "Operator: $ISA_USER (uid $(id -u))"
echo

echo "Validating archive readability..."
tar -tzf "$ARCHIVE" >/dev/null

MANIFEST_CONTENT="$(tar -xzO -f "$ARCHIVE" ./MANIFEST.txt 2>/dev/null || tar -xzO -f "$ARCHIVE" MANIFEST.txt 2>/dev/null || true)"
if [ -z "$MANIFEST_CONTENT" ]; then
  fail "MANIFEST.txt is missing from the backup archive"
  exit 1
fi
EXPECTED_APP_SHA="$(printf '%s\n' "$MANIFEST_CONTENT" | grep -E '^IsadoraAir Git SHA:' | sed -E 's/^IsadoraAir Git SHA:[[:space:]]*//' || true)"
if ! [[ "$EXPECTED_APP_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  fail "backup manifest does not contain a usable IsadoraAir Git SHA"
  exit 1
fi

echo "Backup application SHA: $EXPECTED_APP_SHA"
echo "Extracting embedded recovery code bundles..."
rm -rf "$RECOVERY_ROOT"
mkdir -p "$RECOVERY_ROOT"

# Extract only recovery/. Reject links and path traversal before writing any
# archive member. The backup is trusted operational data, but the bootstrap is
# deliberately fail-closed against malformed member names/types anyway.
python3 - "$ARCHIVE" "$STATE_ROOT" <<'PY'
import pathlib, sys, tarfile
archive = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2]).resolve()
with tarfile.open(archive, "r:gz") as tf:
    selected = []
    for member in tf.getmembers():
        name = member.name[2:] if member.name.startswith("./") else member.name
        if not (name == "recovery" or name.startswith("recovery/")):
            continue
        p = pathlib.PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts:
            raise SystemExit(f"unsafe recovery member path: {member.name}")
        if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
            raise SystemExit(f"unsupported recovery member type: {member.name}")
        member.name = name
        selected.append(member)
    if not selected:
        raise SystemExit("backup does not contain recovery/ payload")
    tf.extractall(out, members=selected, filter="data")
PY

for required in \
  "$RECOVERY_ROOT/SHA256SUMS" \
  "$RECOVERY_ROOT/IsadoraAir.bundle" \
  "$RECOVERY_ROOT/syndicated-ingest.bundle" \
  "$RECOVERY_ROOT/ogremote-ingest.bundle"; do
  if [ ! -s "$required" ]; then
    fail "required embedded recovery item is missing or empty: $required"
    exit 1
  fi
done

(
  cd "$RECOVERY_ROOT"
  sha256sum -c SHA256SUMS
)

for bundle in IsadoraAir.bundle syndicated-ingest.bundle ogremote-ingest.bundle; do
  git bundle verify "$RECOVERY_ROOT/$bundle" >/dev/null
  echo "  $bundle: verified"
done

if ! git bundle list-heads "$RECOVERY_ROOT/IsadoraAir.bundle" | grep -q "^$EXPECTED_APP_SHA "; then
  fail "embedded IsadoraAir.bundle does not advertise the backup's recorded commit $EXPECTED_APP_SHA"
  exit 1
fi

seed_checkout() {
  local bundle="$1" target="$2" label="$3"
  if [ -d "$target/.git" ]; then
    echo "$label checkout already exists at $target; preserving it for resume."
    return 0
  fi
  if [ -e "$target" ] && [ -n "$(ls -A "$target" 2>/dev/null)" ]; then
    fail "$target exists, is non-empty, and is not a Git checkout"
    return 1
  fi
  mkdir -p "$(dirname "$target")"
  git clone "$bundle" "$target"
}

# Establish /opt/isadoraair as the ordinary operator. The only privileged
# operation here is creating/chowning the initially-empty canonical directory.
if [ ! -e /opt/isadoraair ]; then
  sudo mkdir -p /opt/isadoraair
  sudo chown "$ISA_USER:$ISA_GROUP" /opt/isadoraair
elif [ ! -d /opt/isadoraair/.git ] && [ -n "$(ls -A /opt/isadoraair 2>/dev/null)" ]; then
  fail "/opt/isadoraair already exists with non-Git content; refusing to overwrite it"
  exit 1
else
  sudo chown "$ISA_USER:$ISA_GROUP" /opt/isadoraair
fi

if [ ! -d /opt/isadoraair/.git ]; then
  # git clone requires the destination not to exist. /opt itself is root-owned,
  # so create the clone beside the prepared empty directory and move its content
  # in without ever running git as root.
  rmdir /opt/isadoraair
  git clone "$RECOVERY_ROOT/IsadoraAir.bundle" /opt/isadoraair
fi

if ! git -C /opt/isadoraair cat-file -e "${EXPECTED_APP_SHA}^{commit}" 2>/dev/null; then
  fail "seeded IsadoraAir checkout cannot resolve $EXPECTED_APP_SHA"
  exit 1
fi
git -C /opt/isadoraair checkout --detach "$EXPECTED_APP_SHA"

seed_checkout "$RECOVERY_ROOT/syndicated-ingest.bundle" "$HOME/syndicated-ingest" "syndicated-ingest"
seed_checkout "$RECOVERY_ROOT/ogremote-ingest.bundle" "$HOME/ogremote-ingest" "ogremote-ingest"

# Install every optional public OS integration group from ordinary online Ubuntu
# sources. This deliberately trades a small amount of extra installed software
# for a recovery procedure with no station-specific package-selection ceremony.
echo
echo "Installing IsadoraAir OS prerequisites from online Ubuntu/Snap sources..."
/opt/isadoraair/deploy/restore/10-packages.sh \
  --archive "$ARCHIVE" \
  --apply \
  --force-production-target \
  --with-all-optional

echo
echo "Starting canonical bare-metal restore..."
/opt/isadoraair/deploy/restore/bare_metal_restore.sh \
  --archive "$ARCHIVE" \
  --apply \
  --force-production-target \
  --owner "$ISA_USER:$ISA_GROUP" \
  --isa-user "$ISA_USER" \
  --non-interactive

# The embedded bundles are bootstrap authority, not the desired long-term
# remotes after a successful connected restore. Reset origins only after the
# entire bare-metal wrapper (including post-rebind Stage 95) has passed.
git -C /opt/isadoraair remote set-url origin https://github.com/celltech161/IsadoraAir.git
if [ -d "$HOME/syndicated-ingest/.git" ]; then
  git -C "$HOME/syndicated-ingest" remote set-url origin git@github.com:celltech161/syndicated-ingest.git
fi
if [ -d "$HOME/ogremote-ingest/.git" ]; then
  git -C "$HOME/ogremote-ingest" remote set-url origin git@github.com:celltech161/ogremote-ingest.git
fi

echo
echo "Archive-only bare-metal recovery PASS."
echo "Services remain under the restore framework's controlled bring-up boundary."
