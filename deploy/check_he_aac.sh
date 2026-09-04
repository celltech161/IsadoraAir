#!/usr/bin/env bash
# Authoritative fdkaac linkage and LC/HE/HEv2 functional validator.
#
# It checks versions, ELF dependency, exact library resolution, then performs
# real profile 2/5/29 encodes and ffmpeg decodes. The test writes only to its
# private temporary directory and cleans it on exit.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MANIFEST="$REPO_ROOT/isadoraair/runtime_components.json"

usage() {
  cat <<'EOF'
Usage: deploy/check_he_aac.sh [options]
       deploy/check_he_aac.sh [fdkaac_binary] [lib_dir]

Options:
  --prefix PREFIX                    Validate PREFIX/bin and PREFIX/lib.
  --fdkaac PATH                      fdkaac executable to validate.
  --lib-dir PATH                     Intended libfdk-aac directory.
  --expected-fdkaac-version VERSION  Override manifest expectation.
  --expected-libfdk-version VERSION  Override manifest expectation.
  --runtime-only                     Do not require build-only pkg-config metadata.
  -h, --help                         Show this help.

With no arguments, validates canonical production paths from the runtime
manifest. Exit 0 is the install/preflight authority; any version, linkage,
encode, or decode failure exits nonzero with a specific diagnostic.
EOF
}

mapfile -t DEFAULTS < <(python3 - "$MANIFEST" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    component = json.load(source)["components"]["fdkaac"]
runtime = component["runtime"]
print(runtime["binary"])
print(runtime["library_root"])
print(runtime["fdkaac_version"])
print(runtime["libfdk_aac_version"])
PY
)
if [ "${#DEFAULTS[@]}" -ne 4 ]; then
  echo "FAIL: incomplete fdkaac runtime contract in $MANIFEST" >&2
  exit 1
fi

FDKAAC_BIN="${DEFAULTS[0]}"
LIB_DIR="${DEFAULTS[1]}"
EXPECTED_FDKAAC_VERSION="${DEFAULTS[2]}"
EXPECTED_LIBFDK_VERSION="${DEFAULTS[3]}"
PREFIX_VALUE=""
RUNTIME_ONLY=0

if [ "$#" -gt 0 ] && [[ "$1" != -* ]]; then
  FDKAAC_BIN="$1"
  shift
  if [ "$#" -gt 0 ] && [[ "$1" != -* ]]; then
    LIB_DIR="$1"
    shift
  fi
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX_VALUE="${2:?--prefix needs a path}"; shift 2 ;;
    --prefix=*) PREFIX_VALUE="${1#*=}"; shift ;;
    --fdkaac) FDKAAC_BIN="${2:?--fdkaac needs a path}"; shift 2 ;;
    --fdkaac=*) FDKAAC_BIN="${1#*=}"; shift ;;
    --lib-dir) LIB_DIR="${2:?--lib-dir needs a path}"; shift 2 ;;
    --lib-dir=*) LIB_DIR="${1#*=}"; shift ;;
    --expected-fdkaac-version) EXPECTED_FDKAAC_VERSION="${2:?version required}"; shift 2 ;;
    --expected-fdkaac-version=*) EXPECTED_FDKAAC_VERSION="${1#*=}"; shift ;;
    --expected-libfdk-version) EXPECTED_LIBFDK_VERSION="${2:?version required}"; shift 2 ;;
    --expected-libfdk-version=*) EXPECTED_LIBFDK_VERSION="${1#*=}"; shift ;;
    --runtime-only) RUNTIME_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "FAIL: unrecognized argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -n "$PREFIX_VALUE" ]; then
  if [[ "$PREFIX_VALUE" != /* ]]; then
    echo "FAIL: --prefix must be an absolute path" >&2
    exit 2
  fi
  FDKAAC_BIN="$PREFIX_VALUE/bin/fdkaac"
  LIB_DIR="$PREFIX_VALUE/lib"
fi

REQUIRED_TOOLS=(python3 readelf ldd realpath ffmpeg awk sed)
if [ "$RUNTIME_ONLY" -eq 0 ]; then
  REQUIRED_TOOLS+=(pkg-config)
fi
for tool in "${REQUIRED_TOOLS[@]}"; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "FAIL: required validation tool not found on PATH: $tool" >&2
    exit 1
  fi
done
if [ ! -x "$FDKAAC_BIN" ]; then
  echo "FAIL: $FDKAAC_BIN does not exist or is not executable" >&2
  exit 1
fi
if [ ! -d "$LIB_DIR" ]; then
  echo "FAIL: intended library directory does not exist: $LIB_DIR" >&2
  exit 1
fi

VERSION_OUTPUT="$({ LD_LIBRARY_PATH="$LIB_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" "$FDKAAC_BIN" 2>&1 || true; } | sed -n '1p')"
ACTUAL_FDKAAC_VERSION="${VERSION_OUTPUT#fdkaac }"
if [ "$ACTUAL_FDKAAC_VERSION" != "$EXPECTED_FDKAAC_VERSION" ]; then
  echo "FAIL: fdkaac version mismatch" >&2
  echo "  expected: $EXPECTED_FDKAAC_VERSION" >&2
  echo "  actual:   ${ACTUAL_FDKAAC_VERSION:-unknown}" >&2
  exit 1
fi
echo "PASS fdkaac version: $ACTUAL_FDKAAC_VERSION"

EXPECTED_LIBRARY="$LIB_DIR/libfdk-aac.so.$EXPECTED_LIBFDK_VERSION"
if [ ! -f "$EXPECTED_LIBRARY" ]; then
  echo "FAIL: expected libfdk-aac versioned library is missing: $EXPECTED_LIBRARY" >&2
  exit 1
fi
if [ "$RUNTIME_ONLY" -eq 0 ]; then
  ACTUAL_PC_VERSION="$(PKG_CONFIG_PATH="$LIB_DIR/pkgconfig" \
    PKG_CONFIG_LIBDIR="$LIB_DIR/pkgconfig" pkg-config --modversion fdk-aac 2>/dev/null || true)"
  if [ "$ACTUAL_PC_VERSION" != "$EXPECTED_LIBFDK_VERSION" ]; then
    echo "FAIL: staged fdk-aac pkg-config version mismatch" >&2
    echo "  expected: $EXPECTED_LIBFDK_VERSION" >&2
    echo "  actual:   ${ACTUAL_PC_VERSION:-unavailable}" >&2
    exit 1
  fi
  echo "PASS libfdk-aac version: $ACTUAL_PC_VERSION"
else
  echo "PASS libfdk-aac runtime identity: $EXPECTED_LIBRARY"
fi

if ! readelf -d "$FDKAAC_BIN" | awk '/NEEDED/ && /libfdk-aac\.so\.2/ {found=1} END {exit !found}'; then
  echo "FAIL: fdkaac ELF does not declare the required libfdk-aac.so.2 dependency" >&2
  exit 1
fi

LDD_OUTPUT="$(LD_LIBRARY_PATH="$LIB_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ldd "$FDKAAC_BIN")"
RESOLVED_LIBRARY="$(printf '%s\n' "$LDD_OUTPUT" | awk '/libfdk-aac\.so\.2/ {print $3; exit}')"
if [ -z "$RESOLVED_LIBRARY" ] || [ ! -e "$RESOLVED_LIBRARY" ]; then
  echo "FAIL: ldd did not resolve libfdk-aac.so.2" >&2
  printf '%s\n' "$LDD_OUTPUT" >&2
  exit 1
fi
if [ "$(realpath "$RESOLVED_LIBRARY")" != "$(realpath "$EXPECTED_LIBRARY")" ]; then
  echo "FAIL: fdkaac resolved the wrong libfdk-aac" >&2
  echo "  expected: $(realpath "$EXPECTED_LIBRARY")" >&2
  echo "  actual:   $(realpath "$RESOLVED_LIBRARY")" >&2
  exit 1
fi
echo "PASS library linkage: $RESOLVED_LIBRARY -> $(realpath "$EXPECTED_LIBRARY")"

WORKDIR="$(mktemp -d -t isadoraair-he-aac-check.XXXXXX)"
cleanup() {
  status=$?
  rm -rf -- "$WORKDIR"
  return "$status"
}
trap cleanup EXIT

echo "Testing codec capabilities with: $FDKAAC_BIN"
ffmpeg -y -loglevel error -f lavfi \
  -i "sine=frequency=1000:duration=1:sample_rate=44100" \
  -ac 2 -f s16le "$WORKDIR/tone.raw"

declare -A PROFILE_NAME=([2]="AAC-LC" [5]="HE-AAC/SBR" [29]="HE-AACv2/SBR+PS")
FAILED=0

for profile in 2 5 29; do
  name="${PROFILE_NAME[$profile]}"
  output="$WORKDIR/p${profile}.aac"
  encode_log="$WORKDIR/p${profile}.encode.log"
  decode_log="$WORKDIR/p${profile}.decode.log"

  if ! LD_LIBRARY_PATH="$LIB_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
    "$FDKAAC_BIN" -R --raw-channels 2 --raw-rate 44100 --raw-format S16L \
      -p "$profile" -b 64000 -f 2 -a 1 -S -o "$output" "$WORKDIR/tone.raw" \
      > "$encode_log" 2>&1; then
    echo "FAIL profile $profile ($name): encode rejected"
    echo "  $(tail -1 "$encode_log")"
    FAILED=1
    continue
  fi
  if [ ! -s "$output" ]; then
    echo "FAIL profile $profile ($name): encoder produced an empty file"
    FAILED=1
    continue
  fi
  if ! ffmpeg -y -loglevel error -i "$output" -f null - > "$decode_log" 2>&1; then
    echo "FAIL profile $profile ($name): ffmpeg could not decode the output"
    echo "  $(tail -1 "$decode_log")"
    FAILED=1
    continue
  fi
  echo "PASS profile $profile ($name): encoded and ffmpeg decoded"
done

if [ "$FAILED" -ne 0 ]; then
  echo "FAIL: HE-AAC dependency incomplete; see per-profile diagnostics above." >&2
  exit 1
fi

echo "PASS: linkage + AAC-LC + HE-AAC/SBR + HE-AACv2/SBR+PS + ffmpeg decode"
