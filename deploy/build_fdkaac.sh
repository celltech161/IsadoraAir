#!/usr/bin/env bash
# Reproducible build of the HE-AAC / HE-AACv2 encoder chain IsadoraAir
# requires: mstorsjo/fdk-aac (the actual codec library) followed by
# nu774/fdkaac (the CLI frontend encoders/services/encoder_manager.py
# invokes -- see that module's FDKAAC_PATH and its docstring for why a
# self-built copy is required at all).
#
# WHY THIS EXISTS: Ubuntu packages fdkaac + libfdk-aac2, but the packaged
# libfdk-aac2 has SBR (HE-AAC) and Parametric Stereo (HE-AACv2) compiled
# out for legacy software-patent reasons -- profile 5 and profile 29
# both fail with "ERROR: unsupported profile" against it. Confirmed live
# on production (Ubuntu 26.04, libfdk-aac2 2.0.2-3~ubuntu5) during
# IsadoraAir 1.2 Phase 3, 2026-08-12, by forcing LD_LIBRARY_PATH to
# bypass the /usr/local shadow described below and testing directly
# against the true packaged library. Only a self-built libfdk-aac (SBR/PS
# compiled in, which upstream's default build does) fixes this.
#
# PINNED VERSIONS (do not float these without a deliberate decision --
# see docs/HE_AAC_FDKAAC_PROVENANCE.md for the full history; the exact
# original production build's compiler invocation is unrecoverable, so
# this is the new authoritative reproduction recipe going forward):
#   mstorsjo/fdk-aac  v2.0.3
#   nu774/fdkaac      v1.0.7
# Matches the versions already running in production
# (/usr/local/lib/libfdk-aac.so.2.0.3, `fdkaac 1.0.7` per its usage
# banner) -- this script does not change what's running, only makes it
# reproducible from a clean machine.
#
# SAFETY: this script NEVER defaults to installing over the live
# production copy. PREFIX must always be passed explicitly. For
# validation, use a throwaway prefix:
#   PREFIX=/tmp/fdkaac-stage bash deploy/build_fdkaac.sh
# Only pass PREFIX=/usr/local as a deliberate, separate, explicitly-
# approved production step -- this script has no opinion on when that's
# appropriate and will happily overwrite an existing /usr/local install
# if pointed at it, so treat that invocation with the same care as any
# other production change.
#
# A NOTE ON LIBRARY SHADOWING: if a previous build already lives under
# /usr/local/lib/libfdk-aac.so.2, Ubuntu's own /usr/bin/fdkaac (linked
# against libfdk-aac.so.2 by SONAME, not a specific path) will silently
# resolve to THAT copy at runtime rather than its own packaged
# /usr/lib/x86_64-linux-gnu/libfdk-aac.so.2 -- /usr/local/lib precedes
# the multiarch system library directory in the default ld.so search
# order. This is coincidental, not by design (an `ldconfig` cache
# change or removing the /usr/local copy breaks it immediately) -- it
# is NOT a substitute for encoder_manager.py's own FDKAAC_PATH absolute-
# path pin, which exists specifically so the exact binary invoked is
# never ambiguous. Worth knowing if a `/usr/bin/fdkaac -p 29` smoke test
# ever behaves unexpectedly well or badly on a given host.
#
# Required build packages (Ubuntu 26.04):
#   sudo apt install build-essential autoconf automake libtool \
#     pkg-config git
#
# Usage:
#   PREFIX=/tmp/fdkaac-stage bash deploy/build_fdkaac.sh
#   PREFIX=/tmp/fdkaac-stage BUILD_DIR=/tmp/fdkaac-src JOBS=4 bash deploy/build_fdkaac.sh
#
# After building, validate before trusting the result:
#   LD_LIBRARY_PATH="$PREFIX/lib" deploy/check_he_aac.sh "$PREFIX/bin/fdkaac"

set -euo pipefail

FDK_AAC_TAG="v2.0.3"
FDKAAC_TAG="v1.0.7"
FDK_AAC_REPO="https://github.com/mstorsjo/fdk-aac.git"
FDKAAC_REPO="https://github.com/nu774/fdkaac.git"

# Never silently defaults to /usr/local -- see the safety note above.
PREFIX="${PREFIX:?Set PREFIX explicitly. Examples: PREFIX=/tmp/fdkaac-stage bash deploy/build_fdkaac.sh   (validation, recommended)  or  PREFIX=/usr/local bash deploy/build_fdkaac.sh   (production -- only as a separate, deliberate step, never implied by running this script).}"

BUILD_DIR="${BUILD_DIR:-$(mktemp -d -t fdkaac-build.XXXXXX)}"
JOBS="${JOBS:-$(nproc)}"

echo "IsadoraAir HE-AAC build: fdk-aac ${FDK_AAC_TAG} + fdkaac ${FDKAAC_TAG}"
echo "  PREFIX:    ${PREFIX}"
echo "  BUILD_DIR: ${BUILD_DIR}"
echo "  JOBS:      ${JOBS}"
echo

# ---- dependency preflight: fail clearly, never fall back silently --------
MISSING=()
for bin in git gcc g++ make autoconf automake autoreconf pkg-config; do
  command -v "$bin" >/dev/null 2>&1 || MISSING+=("$bin")
done
# The `libtool` *command* only exists per-project after autoreconf
# generates it -- what must actually be installed system-wide is the
# libtool package (libtoolize + its .m4 macros), so check that instead
# of a bare `libtool` on PATH (which is never present standalone even on
# a fully-provisioned build host).
command -v libtoolize >/dev/null 2>&1 || MISSING+=("libtoolize (apt package: libtool)")

if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "Error: missing required build tool(s): ${MISSING[*]}" >&2
  echo "Install with: sudo apt install build-essential autoconf automake libtool pkg-config git" >&2
  exit 1
fi

if [ -e "$BUILD_DIR/fdk-aac" ] || [ -e "$BUILD_DIR/fdkaac" ]; then
  echo "Error: $BUILD_DIR already contains a fdk-aac/ or fdkaac/ checkout." >&2
  echo "Use a fresh BUILD_DIR (or remove the existing one) -- this script" >&2
  echo "never reuses/updates an existing source tree, to keep the build" >&2
  echo "reproducible from a known-clean clone every time." >&2
  exit 1
fi

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"

# ---- fdk-aac (the codec library) ------------------------------------------
echo "Cloning mstorsjo/fdk-aac @ ${FDK_AAC_TAG} ..."
git clone --branch "$FDK_AAC_TAG" --depth 1 "$FDK_AAC_REPO" fdk-aac
cd fdk-aac
FDK_AAC_COMMIT=$(git rev-parse HEAD)
autoreconf -fiv
./configure --prefix="$PREFIX"
make -j"$JOBS"
make install
cd "$BUILD_DIR"

# ---- fdkaac (the CLI frontend, linked against the library just built) ----
echo
echo "Cloning nu774/fdkaac @ ${FDKAAC_TAG} ..."
git clone --branch "$FDKAAC_TAG" --depth 1 "$FDKAAC_REPO" fdkaac
cd fdkaac
FDKAAC_COMMIT=$(git rev-parse HEAD)
autoreconf -fiv
# Point pkg-config at the fdk-aac.pc this same run just installed into
# PREFIX, so a non-standard PREFIX (the whole point of the validation
# path) is actually found rather than silently falling back to whatever
# libfdk-aac.pc (if any) is already on the system pkg-config path.
PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" \
  ./configure --prefix="$PREFIX"
make -j"$JOBS"
make install
cd "$BUILD_DIR"

echo
echo "Build complete."
echo "  fdk-aac commit: $FDK_AAC_COMMIT"
echo "  fdkaac commit:  $FDKAAC_COMMIT"
echo "  Installed to:   $PREFIX"
echo
echo "Binary:  $PREFIX/bin/fdkaac"
echo "Library: $PREFIX/lib/libfdk-aac.so*"
echo
echo "Validate before trusting this build:"
echo "  LD_LIBRARY_PATH=\"$PREFIX/lib\" deploy/check_he_aac.sh \"$PREFIX/bin/fdkaac\""
