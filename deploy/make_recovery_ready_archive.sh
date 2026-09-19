#!/usr/bin/env bash
# Add embedded Git recovery bundles to an existing accepted IsadoraAir backup.
#
# This is deliberately a packaging helper, not a second backup implementation:
# it leaves the supplied archive untouched and emits a new recovery-ready copy.
# Long-term, backup_isadoraair.sh can call the same logic before final archive
# creation. For physical acceptance this lets us prove the final operator UX
# (one archive + one command) without reworking the mature backup pipeline first.
set -euo pipefail

usage() {
  echo "Usage: $0 SOURCE_BACKUP.tar.gz OUTPUT_RECOVERY_READY.tar.gz" >&2
}

fail() {
  echo "ERROR: $*" >&2
  return 1
}

if [ "$#" -ne 2 ]; then
  usage
  exit 2
fi

SOURCE="$(readlink -f "$1")"
OUTPUT="$(readlink -m "$2")"
PROJECT_DIR="${PROJECT_DIR:-/opt/isadoraair}"
SYNDICATED_DIR="${SYNDICATED_DIR:-$HOME/syndicated-ingest}"
OGREMOTE_DIR="${OGREMOTE_DIR:-$HOME/ogremote-ingest}"

for cmd in tar git python3 sha256sum mktemp; do
  command -v "$cmd" >/dev/null 2>&1 || { fail "required command missing: $cmd"; exit 1; }
done
[ -f "$SOURCE" ] || { fail "source archive not found: $SOURCE"; exit 1; }
[ ! -e "$OUTPUT" ] || { fail "output already exists: $OUTPUT"; exit 1; }

for repo in "$PROJECT_DIR" "$SYNDICATED_DIR" "$OGREMOTE_DIR"; do
  [ -d "$repo/.git" ] || { fail "required Git checkout missing: $repo"; exit 1; }
  if git -C "$repo" status --porcelain --untracked-files=no | grep -q .; then
    fail "tracked files are dirty in $repo; refusing to build a recovery authority that cannot exactly reconstruct the running checkout"
    exit 1
  fi
done

tar -tzf "$SOURCE" >/dev/null
MANIFEST_CONTENT="$(tar -xzO -f "$SOURCE" ./MANIFEST.txt 2>/dev/null || tar -xzO -f "$SOURCE" MANIFEST.txt 2>/dev/null || true)"
[ -n "$MANIFEST_CONTENT" ] || { fail "MANIFEST.txt missing from source archive"; exit 1; }
BACKUP_APP_SHA="$(printf '%s\n' "$MANIFEST_CONTENT" | grep -E '^IsadoraAir Git SHA:' | sed -E 's/^IsadoraAir Git SHA:[[:space:]]*//' || true)"
CURRENT_APP_SHA="$(git -C "$PROJECT_DIR" rev-parse HEAD)"
if [ "$BACKUP_APP_SHA" != "$CURRENT_APP_SHA" ]; then
  fail "source backup records IsadoraAir $BACKUP_APP_SHA but current checkout is $CURRENT_APP_SHA; package bundles from the exact backup revision instead"
  exit 1
fi

WORKDIR="$(mktemp -d /tmp/isadoraair-recovery-ready.XXXXXX)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT

mkdir -p "$WORKDIR/archive"
tar -xzf "$SOURCE" -C "$WORKDIR/archive"

if [ -e "$WORKDIR/archive/recovery" ]; then
  fail "source archive already contains recovery/; refusing to overwrite embedded recovery authority"
  exit 1
fi
mkdir -p "$WORKDIR/archive/recovery"

APP_SHA="$CURRENT_APP_SHA"
SYNDICATED_SHA="$(git -C "$SYNDICATED_DIR" rev-parse HEAD)"
OGREMOTE_SHA="$(git -C "$OGREMOTE_DIR" rev-parse HEAD)"

echo "Creating embedded Git bundles..."
git -C "$PROJECT_DIR" bundle create "$WORKDIR/archive/recovery/IsadoraAir.bundle" --all
git -C "$SYNDICATED_DIR" bundle create "$WORKDIR/archive/recovery/syndicated-ingest.bundle" --all
git -C "$OGREMOTE_DIR" bundle create "$WORKDIR/archive/recovery/ogremote-ingest.bundle" --all

# `git bundle verify` requires repository context even for a bundle with no
# prerequisites. Use one disposable bare repository solely as that context;
# nothing is fetched into it and the source checkouts/bundles remain untouched.
VERIFY_REPO="$WORKDIR/bundle-verify.git"
git init --bare -q "$VERIFY_REPO"
for bundle in IsadoraAir.bundle syndicated-ingest.bundle ogremote-ingest.bundle; do
  git -C "$VERIFY_REPO" bundle verify "$WORKDIR/archive/recovery/$bundle" >/dev/null
  echo "  $bundle: verified"
done

python3 - "$WORKDIR/archive/recovery/recovery-manifest.json" "$APP_SHA" "$SYNDICATED_SHA" "$OGREMOTE_SHA" <<'PY'
import datetime, json, pathlib, sys
out = pathlib.Path(sys.argv[1])
payload = {
    "schema_version": 1,
    "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "mode": "archive_only_online_dependencies",
    "repositories": {
        "IsadoraAir": {"commit": sys.argv[2], "bundle": "IsadoraAir.bundle"},
        "syndicated-ingest": {"commit": sys.argv[3], "bundle": "syndicated-ingest.bundle"},
        "ogremote-ingest": {"commit": sys.argv[4], "bundle": "ogremote-ingest.bundle"},
    },
}
out.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
PY

(
  cd "$WORKDIR/archive/recovery"
  sha256sum \
    IsadoraAir.bundle \
    syndicated-ingest.bundle \
    ogremote-ingest.bundle \
    recovery-manifest.json \
    > SHA256SUMS
  sha256sum -c SHA256SUMS
)

echo "Building recovery-ready archive..."
tar czf "$OUTPUT" -C "$WORKDIR/archive" .
tar -tzf "$OUTPUT" >/dev/null

# Prove the embedded payload survived the outer repack intact.
VERIFY_DIR="$WORKDIR/verify"
mkdir -p "$VERIFY_DIR"
python3 - "$OUTPUT" "$VERIFY_DIR" <<'PY'
import pathlib, sys, tarfile
archive = pathlib.Path(sys.argv[1])
out = pathlib.Path(sys.argv[2])
with tarfile.open(archive, "r:gz") as tf:
    members = []
    for m in tf.getmembers():
        name = m.name[2:] if m.name.startswith("./") else m.name
        if name == "recovery" or name.startswith("recovery/"):
            m.name = name
            members.append(m)
    tf.extractall(out, members=members, filter="data")
PY
(
  cd "$VERIFY_DIR/recovery"
  sha256sum -c SHA256SUMS
)

OUTPUT_SHA="$(sha256sum "$OUTPUT" | awk '{print $1}')"
OUTPUT_BYTES="$(stat -c%s "$OUTPUT" 2>/dev/null || stat -f%z "$OUTPUT")"

echo
echo "Recovery-ready archive created:"
echo "  $OUTPUT"
echo "  bytes:  $OUTPUT_BYTES"
echo "  sha256: $OUTPUT_SHA"
echo "  IsadoraAir:        $APP_SHA"
echo "  syndicated-ingest: $SYNDICATED_SHA"
echo "  ogremote-ingest:   $OGREMOTE_SHA"
