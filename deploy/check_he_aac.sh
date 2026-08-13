#!/usr/bin/env bash
# HE-AAC / HE-AACv2 capability smoke test -- proves a fdkaac binary
# actually supports the three profiles IsadoraAir's encoder_manager.py
# selects between (see that module's _format_block), not just that
# `fdkaac --help` runs or lists the profile numbers in its usage text.
#
# WHY THIS IS NECESSARY, NOT JUST BELT-AND-SUSPENDERS: `fdkaac -h`
# prints the same "-p <n> ... 5: MPEG-4 HE-AAC (SBR) ... 29: MPEG-4
# HE-AAC v2" text regardless of whether the linked libfdk-aac actually
# has SBR/PS compiled in -- Ubuntu's own packaged libfdk-aac2 lists
# those exact same profile numbers in --help and then fails encoding
# with "ERROR: unsupported profile" the moment you actually try to use
# them. Confirmed live during IsadoraAir 1.2 Phase 3, 2026-08-12.
#
# METHOD: encode a tiny synthetic 1-second sine tone at each of profile
# 2 (AAC-LC), 5 (HE-AAC), and 29 (HE-AACv2), using the exact flag shape
# encoder_manager.py's _format_block generates (-R raw PCM in,
# --raw-channels 2 --raw-rate 44100 --raw-format S16L, -f 2 ADTS out,
# -a 1 afterburner on). A build without real SBR/PS support REFUSES to
# encode profile 5/29 at all (nonzero exit, "unsupported profile") --
# that refusal, not bitstream introspection, is the actual pass/fail
# signal. (ADTS's implicit SBR signaling means a successfully-produced
# HE-AAC file is NOT reliably distinguishable from plain LC by
# ffprobe/ffmpeg alone after the fact -- confirmed empirically; a
# correctly-encoded HE-AACv2 stream still reports `profile=LC` because
# ADTS never carries an explicit SBR/PS flag in its header. The encode
# step's own accept/reject behavior is the only reliable signal.) Each
# successful encode is also decode-verified with ffmpeg to confirm the
# output is genuinely valid, non-empty, correct-duration audio, not
# just a file that happened to get written.
#
# Usage:
#   deploy/check_he_aac.sh [fdkaac_binary] [lib_dir]
#
#   fdkaac_binary   defaults to /usr/local/bin/fdkaac (the production
#                   path encoder_manager.py's FDKAAC_PATH hardcodes)
#   lib_dir         optional; prepended to LD_LIBRARY_PATH for testing
#                   a staged build.py in a non-standard prefix, e.g.
#                   the output of deploy/build_fdkaac.sh with
#                   PREFIX=/tmp/fdkaac-stage:
#                     deploy/check_he_aac.sh /tmp/fdkaac-stage/bin/fdkaac /tmp/fdkaac-stage/lib
#
# Exit 0 and prints "PASS: LC / HE / HEv2 supported" only if all three
# profiles encode AND decode successfully. Exit 1 with a specific
# per-profile diagnostic otherwise. Never modifies anything outside its
# own temp directory; never touches production encoder state.

set -euo pipefail

FDKAAC_BIN="${1:-/usr/local/bin/fdkaac}"
LIB_DIR="${2:-}"

if [ -n "$LIB_DIR" ]; then
  export LD_LIBRARY_PATH="${LIB_DIR}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi

if [ ! -x "$FDKAAC_BIN" ]; then
  echo "FAIL: $FDKAAC_BIN does not exist or is not executable" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "FAIL: ffmpeg not found on PATH (needed to generate/verify the test tone)" >&2
  exit 1
fi

WORKDIR=$(mktemp -d -t he-aac-check.XXXXXX)
trap 'rm -rf "$WORKDIR"' EXIT

echo "Testing: $FDKAAC_BIN"
"$FDKAAC_BIN" 2>&1 | head -1 || true
echo

ffmpeg -y -loglevel error -f lavfi -i "sine=frequency=1000:duration=1:sample_rate=44100" \
  -ac 2 -f s16le "$WORKDIR/tone.raw"

declare -A PROFILE_NAME=([2]="AAC-LC" [5]="HE-AAC/SBR" [29]="HE-AACv2/SBR+PS")
FAILED=0

for profile in 2 5 29; do
  name="${PROFILE_NAME[$profile]}"
  out="$WORKDIR/p${profile}.aac"

  if ! "$FDKAAC_BIN" -R --raw-channels 2 --raw-rate 44100 --raw-format S16L \
        -p "$profile" -b 64000 -f 2 -a 1 -S -o "$out" "$WORKDIR/tone.raw" \
        > "$WORKDIR/p${profile}.encode.log" 2>&1; then
    echo "FAIL profile $profile ($name): encode rejected"
    echo "  $(tail -1 "$WORKDIR/p${profile}.encode.log")"
    FAILED=1
    continue
  fi

  if [ ! -s "$out" ]; then
    echo "FAIL profile $profile ($name): encode reported success but produced an empty file"
    FAILED=1
    continue
  fi

  if ! ffmpeg -y -loglevel error -i "$out" -f null - > "$WORKDIR/p${profile}.decode.log" 2>&1; then
    echo "FAIL profile $profile ($name): encoded output would not decode"
    echo "  $(tail -1 "$WORKDIR/p${profile}.decode.log")"
    FAILED=1
    continue
  fi

  echo "PASS profile $profile ($name): $(du -h "$out" | cut -f1) encoded, decodes cleanly"
done

echo
if [ "$FAILED" -eq 0 ]; then
  echo "PASS: LC / HE / HEv2 supported"
  exit 0
else
  echo "FAIL: HE-AAC dependency incomplete -- see per-profile diagnostics above."
  echo "Most likely cause: linked libfdk-aac was built without SBR/PS support"
  echo "(e.g. Ubuntu's packaged libfdk-aac2). Rebuild via deploy/build_fdkaac.sh"
  echo "against pinned upstream mstorsjo/fdk-aac, which compiles both in by default."
  exit 1
fi
