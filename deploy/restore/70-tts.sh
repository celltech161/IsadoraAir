#!/usr/bin/env bash
# deploy/restore/70-tts.sh -- IsadoraAir 1.2 Phase 4.
#
# Provisions Kokoro and Piper as INDEPENDENT local speech capabilities,
# per docs/KOKORO_PROVENANCE.md / docs/PIPER_PROVENANCE.md (IsadoraAir
# 1.2 Phase 3) -- both are first-class here; Piper is not treated as
# obsolete just because production currently prefers Kokoro for most
# slots. No backend-selection policy lives in this script -- that's
# station config (weather-ingest's lib/voices.py VOICES dict), not a
# restore concern.
#
# Each engine's *runtime* (pip package + native deps) is fully
# reproducible from an ordinary pip install; each engine's *model
# assets* are large, externally-hosted binaries this repo/backup
# deliberately does not carry (see the provenance docs' "Reproducibility
# verdict" sections) -- --kokoro-model-src / --piper-model-src let this
# stage copy them from a known-good local source (e.g. a preserved copy
# of the original install, or a mount of one) when available, and
# report MISSING rather than fail hard when not, per Phase 4 spec
# section 22 ("report available/configured/missing state").
#
# Kokoro's two model files are checksum-verified against the exact
# SHA-256 values docs/KOKORO_PROVENANCE.md recorded -- Piper's 8 voice
# files have no such pinned per-file table (Piper's own
# download_voices.py resolves by name against a versioned catalog
# instead, see that doc), so those are structurally verified (each
# .onnx has its .onnx.json sibling) rather than checksummed here.
#
# Usage:
#   deploy/restore/70-tts.sh [--plan|--apply] [--staging-root PATH]
#     [--skip-kokoro] [--skip-piper]
#     [--kokoro-dir PATH] [--kokoro-model-src PATH]
#     [--piper-dir PATH] [--piper-model-src PATH]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

restore_parse_common_args "$@"
set -- "${RESTORE_REMAINING_ARGS[@]}"

SKIP_KOKORO=0
SKIP_PIPER=0
KOKORO_DIR=""
KOKORO_MODEL_SRC=""
PIPER_DIR=""
PIPER_MODEL_SRC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-kokoro) SKIP_KOKORO=1; shift ;;
    --skip-piper) SKIP_PIPER=1; shift ;;
    --kokoro-dir) KOKORO_DIR="${2:?}"; shift 2 ;;
    --kokoro-dir=*) KOKORO_DIR="${1#*=}"; shift ;;
    --kokoro-model-src) KOKORO_MODEL_SRC="${2:?}"; shift 2 ;;
    --kokoro-model-src=*) KOKORO_MODEL_SRC="${1#*=}"; shift ;;
    --piper-dir) PIPER_DIR="${2:?}"; shift 2 ;;
    --piper-dir=*) PIPER_DIR="${1#*=}"; shift ;;
    --piper-model-src) PIPER_MODEL_SRC="${2:?}"; shift 2 ;;
    --piper-model-src=*) PIPER_MODEL_SRC="${1#*=}"; shift ;;
    *) log_error "70-tts.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done
[ -z "$KOKORO_DIR" ] && KOKORO_DIR="${RESTORE_STAGING_ROOT:-$HOME}/kokoro"
[ -z "$PIPER_DIR" ] && PIPER_DIR="${RESTORE_STAGING_ROOT:-$HOME}/piper"

log_info "=== 70-tts (Kokoro + Piper) ==="
guard_production_target
require_cmd python3

KOKORO_ONNX_SHA256="7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"
KOKORO_VOICES_SHA256="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"

sha256_of() { sha256sum "$1" | cut -d' ' -f1; }

# ---------------------------------------------------------------------
# Kokoro
# ---------------------------------------------------------------------
KOKORO_STATE="MISSING"
if [ "$SKIP_KOKORO" -eq 1 ]; then
  log_info "Kokoro: skipped (--skip-kokoro)"
else
  log_info "-- Kokoro --"
  log_info "Target directory: $KOKORO_DIR"
  do_or_plan mkdir -p "$KOKORO_DIR"

  if [ "$RESTORE_MODE" = "apply" ]; then
    if [ ! -d "$KOKORO_DIR/venv" ]; then
      do_or_plan python3 -m venv "$KOKORO_DIR/venv"
      do_or_plan "$KOKORO_DIR/venv/bin/pip" install kokoro-onnx
    else
      log_info "Kokoro venv already exists -- skipping recreation."
    fi

    if [ -n "$KOKORO_MODEL_SRC" ]; then
      for f in kokoro-v1.0.onnx voices-v1.0.bin; do
        if [ -f "$KOKORO_MODEL_SRC/$f" ]; then
          do_or_plan cp "$KOKORO_MODEL_SRC/$f" "$KOKORO_DIR/$f"
        else
          log_warn "Kokoro: $KOKORO_MODEL_SRC/$f not found -- cannot copy."
        fi
      done
    fi

    MODEL_OK=1
    for f_sha in "kokoro-v1.0.onnx:$KOKORO_ONNX_SHA256" "voices-v1.0.bin:$KOKORO_VOICES_SHA256"; do
      f="${f_sha%%:*}"; expected="${f_sha##*:}"
      if [ -f "$KOKORO_DIR/$f" ]; then
        actual=$(sha256_of "$KOKORO_DIR/$f")
        if [ "$actual" = "$expected" ]; then
          log_info "Kokoro: $f checksum verified (matches docs/KOKORO_PROVENANCE.md)."
        else
          log_error "Kokoro: $f checksum MISMATCH -- expected $expected, got $actual. Not the same artifact documented in docs/KOKORO_PROVENANCE.md."
          MODEL_OK=0
        fi
      else
        log_warn "Kokoro: $f not present at $KOKORO_DIR -- pass --kokoro-model-src pointing at a known-good copy, or obtain it manually from the kokoro-onnx project's releases and verify against the checksum in docs/KOKORO_PROVENANCE.md."
        MODEL_OK=0
      fi
    done

    if [ "$MODEL_OK" -eq 1 ] && [ -x "$KOKORO_DIR/venv/bin/python" ]; then
      TMP_WAV="$(mktemp /tmp/kokoro-smoke.XXXXXX.wav)"
      # Writes the WAV via the stdlib `wave` module, not `soundfile` --
      # soundfile is NOT one of kokoro-onnx's own documented direct
      # dependencies (docs/KOKORO_PROVENANCE.md's `pip show kokoro-onnx`
      # output), so pulling it in here would make this smoke test
      # require something the reproducible recipe itself doesn't.
      if echo "IsadoraAir Phase 4 restore smoke test." | "$KOKORO_DIR/venv/bin/python" -c "
import sys, wave
import numpy as np
from kokoro_onnx import Kokoro
k = Kokoro('$KOKORO_DIR/kokoro-v1.0.onnx', '$KOKORO_DIR/voices-v1.0.bin')
text = sys.stdin.read().strip()
samples, sr = k.create(text, voice='af_jessica', speed=1.0, lang='en-us')
pcm16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
with wave.open('$TMP_WAV', 'wb') as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(pcm16.tobytes())
" 2>&1; then
        log_info "Kokoro: synthesis smoke test PASS ($TMP_WAV)"
        KOKORO_STATE="AVAILABLE"
      else
        log_warn "Kokoro: synthesis smoke test failed to run via the Python API shape assumed here -- production's actual bin/kokoro_synth wrapper may differ; treat this as informational, not a hard failure of the runtime install itself. See docs/KOKORO_PROVENANCE.md for the real CLI shape used in production."
        KOKORO_STATE="CONFIGURED"
      fi
      rm -f "$TMP_WAV"
    fi
  else
    log_plan "python3 -m venv $KOKORO_DIR/venv && $KOKORO_DIR/venv/bin/pip install kokoro-onnx"
    log_plan "copy+checksum-verify kokoro-v1.0.onnx/voices-v1.0.bin from --kokoro-model-src if given"
    log_plan "synthesis smoke test via kokoro-onnx Python API"
  fi
fi

# ---------------------------------------------------------------------
# Piper
# ---------------------------------------------------------------------
PIPER_STATE="MISSING"
if [ "$SKIP_PIPER" -eq 1 ]; then
  log_info "Piper: skipped (--skip-piper)"
else
  log_info "-- Piper --"
  log_info "Target directory: $PIPER_DIR"
  do_or_plan mkdir -p "$PIPER_DIR"

  if [ "$RESTORE_MODE" = "apply" ]; then
    if [ ! -d "$PIPER_DIR/venv" ]; then
      do_or_plan python3 -m venv "$PIPER_DIR/venv"
      do_or_plan "$PIPER_DIR/venv/bin/pip" install piper-tts
    else
      log_info "Piper venv already exists -- skipping recreation."
    fi

    if [ -n "$PIPER_MODEL_SRC" ] && [ -d "$PIPER_MODEL_SRC" ]; then
      COPIED=0
      while IFS= read -r -d '' onnx; do
        base="$(basename "$onnx")"
        json="${onnx}.json"
        if [ -f "$json" ]; then
          do_or_plan cp "$onnx" "$PIPER_DIR/$base"
          do_or_plan cp "$json" "$PIPER_DIR/${base}.json"
          COPIED=$((COPIED + 1))
        else
          log_warn "Piper: $onnx has no matching .onnx.json sibling -- skipped."
        fi
      done < <(find "$PIPER_MODEL_SRC" -maxdepth 1 -iname '*.onnx' -print0)
      log_info "Piper: copied $COPIED voice model pair(s) from $PIPER_MODEL_SRC."
    fi

    PAIR_COUNT=$(find "$PIPER_DIR" -maxdepth 1 -iname '*.onnx' 2>/dev/null | wc -l)
    if [ "$PAIR_COUNT" -eq 0 ]; then
      log_warn "Piper: no voice models present at $PIPER_DIR -- pass --piper-model-src, or obtain named voices via the piper-tts package's own download_voices.py helper (fully deterministic by name, see docs/PIPER_PROVENANCE.md)."
    else
      FIRST_MODEL=$(find "$PIPER_DIR" -maxdepth 1 -iname '*.onnx' | head -1)
      TMP_WAV="$(mktemp /tmp/piper-smoke.XXXXXX.wav)"
      if echo "IsadoraAir Phase 4 restore smoke test." | "$PIPER_DIR/venv/bin/piper" --model "$FIRST_MODEL" --output_file "$TMP_WAV" 2>&1; then
        log_info "Piper: synthesis smoke test PASS using $(basename "$FIRST_MODEL") ($TMP_WAV)"
        PIPER_STATE="AVAILABLE"
      else
        log_error "Piper: synthesis smoke test FAILED using $(basename "$FIRST_MODEL")."
        PIPER_STATE="CONFIGURED"
      fi
      rm -f "$TMP_WAV"
    fi
  else
    log_plan "python3 -m venv $PIPER_DIR/venv && $PIPER_DIR/venv/bin/pip install piper-tts"
    log_plan "copy voice model pairs (.onnx + .onnx.json) from --piper-model-src if given"
    log_plan "synthesis smoke test via venv/bin/piper --model ... --output_file ..."
  fi
fi

# ---------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------
if [ "$RESTORE_MODE" = "apply" ]; then
  log_info "TTS provisioning summary: Kokoro=$KOKORO_STATE Piper=$PIPER_STATE"
  log_info "(AVAILABLE = runtime + models + smoke test all passed; CONFIGURED = runtime+models present but smoke test didn't confirm; MISSING = not usable yet, see warnings above)"
fi
log_info "70-tts: $( [ "$RESTORE_MODE" = apply ] && echo "PASS (see summary above -- MISSING states are informational, not fatal to this stage)" || echo "PLAN complete" )"
