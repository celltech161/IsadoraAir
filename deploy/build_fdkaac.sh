#!/usr/bin/env bash
# Build IsadoraAir's pinned fdk-aac/fdkaac stack from immutable archives.
#
# The component versions, archive names, sizes, hashes, acquisition URLs, and
# validator path come from isadoraair/runtime_components.json. Local-source
# mode is the disaster-recovery authority and never attempts network access:
#
#   deploy/build_fdkaac.sh --source-dir /path/to/native/fdkaac \
#     --prefix /tmp/fdkaac-stage
#
# Optional acquisition for an ordinary connected install is explicit:
#
#   deploy/build_fdkaac.sh --download-sources --prefix /tmp/fdkaac-stage
#
# The build always uses one archive verification/extraction/build/install/
# validation path after acquisition. It never defaults to /usr/local; that
# prefix additionally requires --allow-production-prefix. This script does
# not run ldconfig. See docs/HE_AAC_FDKAAC_PROVENANCE.md.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST="$REPO_ROOT/isadoraair/runtime_components.json"

usage() {
  cat <<'EOF'
Usage:
  deploy/build_fdkaac.sh --source-dir DIR --prefix PREFIX [options]
  deploy/build_fdkaac.sh --download-sources --prefix PREFIX [options]
  deploy/build_fdkaac.sh --print-source-contract

Source modes (choose exactly one):
  --source-dir DIR          Read exact immutable archives from DIR; never network.
  --download-sources       Download pinned archives, then use the same build path.

Build/install options:
  --prefix PREFIX          Required installation prefix (never defaults to /usr/local).
  --build-dir DIR          Use and retain an empty caller-owned workspace.
  --jobs N                 Parallel make jobs (default: nproc).
  --keep-build-dir         Retain an automatically-created workspace.
  --prepare-only           Verify and extract sources, but do not build or install.
  --allow-production-prefix
                           Required in addition to --prefix /usr/local.
  --print-source-contract  Print the manifest-derived archive contract and exit.
  -h, --help               Show this help.

Environment compatibility: PREFIX, BUILD_DIR, and JOBS are honored when their
equivalent command-line option is omitted. Command-line options take precedence.
EOF
}

if [ ! -f "$MANIFEST" ]; then
  echo "Error: runtime component manifest not found: $MANIFEST" >&2
  exit 1
fi

# Python is a baseline IsadoraAir dependency. Reading the checked-in JSON here
# keeps one version/hash authority without adding jq or duplicating constants.
mapfile -t CONTRACT < <(python3 - "$MANIFEST" <<'PY'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    manifest = json.load(source)
component = manifest["components"]["fdkaac"]
runtime = component["runtime"]
archives = component["source_archives"]
values = (
    runtime["libfdk_aac_version"],
    runtime["fdkaac_version"],
    archives["fdk-aac"]["filename"],
    str(archives["fdk-aac"]["bytes"]),
    archives["fdk-aac"]["sha256"],
    archives["fdk-aac"]["acquisition_url"],
    archives["fdk-aac"]["license_file"],
    archives["fdkaac"]["filename"],
    str(archives["fdkaac"]["bytes"]),
    archives["fdkaac"]["sha256"],
    archives["fdkaac"]["acquisition_url"],
    archives["fdkaac"]["license_file"],
    component["build"]["validator"],
    os.path.dirname(os.path.dirname(runtime["binary"])),
)
print(*values, sep="\n")
PY
)

if [ "${#CONTRACT[@]}" -ne 14 ]; then
  echo "Error: incomplete fdkaac contract in $MANIFEST" >&2
  exit 1
fi

FDK_AAC_VERSION="${CONTRACT[0]}"
FDKAAC_VERSION="${CONTRACT[1]}"
FDK_AAC_ARCHIVE="${CONTRACT[2]}"
FDK_AAC_BYTES="${CONTRACT[3]}"
FDK_AAC_SHA256="${CONTRACT[4]}"
FDK_AAC_URL="${CONTRACT[5]}"
FDK_AAC_LICENSE="${CONTRACT[6]}"
FDKAAC_ARCHIVE="${CONTRACT[7]}"
FDKAAC_BYTES="${CONTRACT[8]}"
FDKAAC_SHA256="${CONTRACT[9]}"
FDKAAC_URL="${CONTRACT[10]}"
FDKAAC_LICENSE="${CONTRACT[11]}"
VALIDATOR="$REPO_ROOT/${CONTRACT[12]}"
PRODUCTION_PREFIX="${CONTRACT[13]}"

SOURCE_DIR=""
DOWNLOAD_SOURCES=0
PREFIX_VALUE="${PREFIX:-}"
BUILD_DIR_VALUE="${BUILD_DIR:-}"
JOBS_VALUE="${JOBS:-$(nproc)}"
KEEP_BUILD_DIR=0
PREPARE_ONLY=0
ALLOW_PRODUCTION_PREFIX=0
PRINT_SOURCE_CONTRACT=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source-dir) SOURCE_DIR="${2:?--source-dir needs a path}"; shift 2 ;;
    --source-dir=*) SOURCE_DIR="${1#*=}"; shift ;;
    --download-sources) DOWNLOAD_SOURCES=1; shift ;;
    --prefix) PREFIX_VALUE="${2:?--prefix needs a path}"; shift 2 ;;
    --prefix=*) PREFIX_VALUE="${1#*=}"; shift ;;
    --build-dir) BUILD_DIR_VALUE="${2:?--build-dir needs a path}"; shift 2 ;;
    --build-dir=*) BUILD_DIR_VALUE="${1#*=}"; shift ;;
    --jobs) JOBS_VALUE="${2:?--jobs needs a number}"; shift 2 ;;
    --jobs=*) JOBS_VALUE="${1#*=}"; shift ;;
    --keep-build-dir) KEEP_BUILD_DIR=1; shift ;;
    --prepare-only) PREPARE_ONLY=1; shift ;;
    --allow-production-prefix) ALLOW_PRODUCTION_PREFIX=1; shift ;;
    --print-source-contract) PRINT_SOURCE_CONTRACT=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Error: unrecognized argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "$PRINT_SOURCE_CONTRACT" -eq 1 ]; then
  printf 'fdk-aac\t%s\t%s\t%s\t%s\n' \
    "$FDK_AAC_VERSION" "$FDK_AAC_ARCHIVE" "$FDK_AAC_BYTES" "$FDK_AAC_SHA256"
  printf 'fdkaac\t%s\t%s\t%s\t%s\n' \
    "$FDKAAC_VERSION" "$FDKAAC_ARCHIVE" "$FDKAAC_BYTES" "$FDKAAC_SHA256"
  exit 0
fi

if { [ -n "$SOURCE_DIR" ] && [ "$DOWNLOAD_SOURCES" -eq 1 ]; } || \
   { [ -z "$SOURCE_DIR" ] && [ "$DOWNLOAD_SOURCES" -eq 0 ]; }; then
  echo "Error: choose exactly one source mode: --source-dir DIR or --download-sources" >&2
  exit 2
fi
if [ -z "$PREFIX_VALUE" ] && [ "$PREPARE_ONLY" -ne 1 ]; then
  echo "Error: --prefix is required (and never defaults to $PRODUCTION_PREFIX)" >&2
  exit 2
fi
if [ -n "$PREFIX_VALUE" ] && [ "$PREFIX_VALUE" = "$PRODUCTION_PREFIX" ] && \
   [ "$ALLOW_PRODUCTION_PREFIX" -ne 1 ]; then
  echo "Error: refusing production prefix $PRODUCTION_PREFIX without --allow-production-prefix" >&2
  exit 2
fi
if ! [[ "$JOBS_VALUE" =~ ^[1-9][0-9]*$ ]]; then
  echo "Error: --jobs must be a positive integer" >&2
  exit 2
fi
if [ -n "$PREFIX_VALUE" ] && [[ "$PREFIX_VALUE" != /* ]]; then
  echo "Error: --prefix must be an absolute path" >&2
  exit 2
fi

AUTO_WORKSPACE=0
if [ -n "$BUILD_DIR_VALUE" ]; then
  WORKSPACE="$BUILD_DIR_VALUE"
  if [ -e "$WORKSPACE" ] && [ -n "$(find "$WORKSPACE" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    echo "Error: --build-dir must be absent or empty: $WORKSPACE" >&2
    exit 1
  fi
  mkdir -p "$WORKSPACE"
else
  WORKSPACE="$(mktemp -d "${TMPDIR:-/var/tmp}/isadoraair-fdkaac-build.XXXXXX")"
  AUTO_WORKSPACE=1
fi

cleanup() {
  status=$?
  if [ "$AUTO_WORKSPACE" -eq 1 ] && [ "$KEEP_BUILD_DIR" -ne 1 ]; then
    rm -rf -- "$WORKSPACE"
  else
    echo "Build workspace retained: $WORKSPACE"
  fi
  return "$status"
}
trap cleanup EXIT

require_tools() {
  local missing=()
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "Error: missing required tool(s): ${missing[*]}" >&2
    echo "Ubuntu 26.04 package authority: deploy/packages-ubuntu-26.04.txt BUILD_HEAAC" >&2
    exit 1
  fi
}

require_tools python3 tar sha256sum

if [ "$DOWNLOAD_SOURCES" -eq 1 ]; then
  require_tools curl
  SOURCE_DIR="$WORKSPACE/downloads"
  mkdir -p "$SOURCE_DIR"
  echo "Acquiring pinned archives over HTTPS (optional connected-install mode)..."
  curl --fail --location --proto '=https' --tlsv1.2 \
    --output "$SOURCE_DIR/$FDK_AAC_ARCHIVE" "$FDK_AAC_URL"
  curl --fail --location --proto '=https' --tlsv1.2 \
    --output "$SOURCE_DIR/$FDKAAC_ARCHIVE" "$FDKAAC_URL"
else
  if [ ! -d "$SOURCE_DIR" ]; then
    echo "Error: local source directory does not exist: $SOURCE_DIR" >&2
    exit 1
  fi
  SOURCE_DIR="$(cd "$SOURCE_DIR" && pwd)"
  echo "Using local immutable source archives: $SOURCE_DIR"
  echo "Network acquisition is disabled in --source-dir mode."
fi

verify_archive() {
  local archive_path="$1"
  local expected_name="$2"
  local expected_bytes="$3"
  local expected_sha256="$4"
  local actual_bytes actual_sha256

  if [ ! -f "$archive_path" ]; then
    echo "Error: missing required source archive: $archive_path" >&2
    exit 1
  fi
  actual_sha256="$(sha256sum "$archive_path" | awk '{print $1}')"
  if [ "$actual_sha256" != "$expected_sha256" ]; then
    echo "Error: SHA-256 mismatch for $expected_name" >&2
    echo "  expected: $expected_sha256" >&2
    echo "  actual:   $actual_sha256" >&2
    exit 1
  fi
  actual_bytes="$(wc -c < "$archive_path")"
  if [ "$actual_bytes" -ne "$expected_bytes" ]; then
    echo "Error: byte-count mismatch for $expected_name" >&2
    echo "  expected: $expected_bytes" >&2
    echo "  actual:   $actual_bytes" >&2
    exit 1
  fi
  echo "Verified $expected_name ($actual_bytes bytes, SHA-256 $actual_sha256)"
}

FDK_AAC_ARCHIVE_PATH="$SOURCE_DIR/$FDK_AAC_ARCHIVE"
FDKAAC_ARCHIVE_PATH="$SOURCE_DIR/$FDKAAC_ARCHIVE"
verify_archive "$FDK_AAC_ARCHIVE_PATH" "$FDK_AAC_ARCHIVE" "$FDK_AAC_BYTES" "$FDK_AAC_SHA256"
verify_archive "$FDKAAC_ARCHIVE_PATH" "$FDKAAC_ARCHIVE" "$FDKAAC_BYTES" "$FDKAAC_SHA256"

FDK_AAC_SOURCE="$WORKSPACE/fdk-aac"
FDKAAC_SOURCE="$WORKSPACE/fdkaac"
mkdir "$FDK_AAC_SOURCE" "$FDKAAC_SOURCE"
tar -xzf "$FDK_AAC_ARCHIVE_PATH" --strip-components=1 -C "$FDK_AAC_SOURCE"
tar -xzf "$FDKAAC_ARCHIVE_PATH" --strip-components=1 -C "$FDKAAC_SOURCE"

for required in \
  "$FDK_AAC_SOURCE/configure.ac" \
  "$FDK_AAC_SOURCE/$FDK_AAC_LICENSE" \
  "$FDKAAC_SOURCE/configure.ac" \
  "$FDKAAC_SOURCE/$FDKAAC_LICENSE"; do
  if [ ! -f "$required" ]; then
    echo "Error: verified archive extracted without expected source/license file: $required" >&2
    exit 1
  fi
done
echo "Extracted verified sources into isolated workspace: $WORKSPACE"

if [ "$PREPARE_ONLY" -eq 1 ]; then
  echo "Source preparation complete; build/install skipped by --prepare-only."
  exit 0
fi

require_tools gcc g++ make autoconf automake autoreconf libtoolize pkg-config readelf ldd realpath ffmpeg
if [ ! -x "$VALIDATOR" ]; then
  echo "Error: component validator is missing or not executable: $VALIDATOR" >&2
  exit 1
fi

echo
echo "IsadoraAir HE-AAC build: fdk-aac $FDK_AAC_VERSION + fdkaac $FDKAAC_VERSION"
echo "  PREFIX:    $PREFIX_VALUE"
echo "  WORKSPACE: $WORKSPACE"
echo "  JOBS:      $JOBS_VALUE"

(
  cd "$FDK_AAC_SOURCE"
  autoreconf -fiv
  ./configure --prefix="$PREFIX_VALUE"
  make -j"$JOBS_VALUE"
  make install
)

(
  cd "$FDKAAC_SOURCE"
  autoreconf -fiv
  PKG_CONFIG_PATH="$PREFIX_VALUE/lib/pkgconfig" \
  PKG_CONFIG_LIBDIR="$PREFIX_VALUE/lib/pkgconfig" \
  CPPFLAGS="-I$PREFIX_VALUE/include" \
  LDFLAGS="-L$PREFIX_VALUE/lib" \
    ./configure --prefix="$PREFIX_VALUE"
  make -j"$JOBS_VALUE"
  make install
)

echo
echo "Build/install complete. Running authoritative linkage and codec validator..."
"$VALIDATOR" \
  --fdkaac "$PREFIX_VALUE/bin/fdkaac" \
  --lib-dir "$PREFIX_VALUE/lib" \
  --expected-fdkaac-version "$FDKAAC_VERSION" \
  --expected-libfdk-version "$FDK_AAC_VERSION"

echo
echo "PASS: pinned fdkaac stack built, installed, linked, and capability-validated"
echo "  Binary:  $PREFIX_VALUE/bin/fdkaac"
echo "  Library: $PREFIX_VALUE/lib/libfdk-aac.so.$FDK_AAC_VERSION"
if [ "$PREFIX_VALUE" = "$PRODUCTION_PREFIX" ]; then
  echo "  Production follow-up still required: run ldconfig, then re-run the validator without --lib-dir."
fi
