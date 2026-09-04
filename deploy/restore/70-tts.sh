#!/usr/bin/env bash
# deploy/restore/70-tts.sh -- IsadoraAir 1.2 Phase 4 / Runtime Foundation E7B.
#
# Two entirely separate modes, chosen automatically (never mixed):
#
#   Backup-based disaster recovery (--archive was given, and
#   --legacy-connected-install was not passed): locates this restore's
#   embedded Runtime Foundation E7 recovery payload (via lib.sh's
#   restore_locate_recovery_payload -- the one shared contract stages
#   50/70 both use, see docs/DISASTER_RECOVERY_RESTORE.md), validates
#   it, then delegates the embedded E3 TTS bundle to the REAL Runtime
#   Foundation E3 authority (monitoring/management/commands/
#   provision_runtime_components.py, via --recovery-payload). This
#   stage does not build a venv itself, does not pip install anything,
#   does not re-implement E3's verification, and NEVER falls back to
#   pip/PyPI acquisition just because the payload is missing -- a legacy/
#   v2.x or explicitly non-self-contained archive fails this backup-based
#   stage plainly (Runtime Foundation E7B task step 16 -- see "Backward
#   compatibility" in docs/DISASTER_RECOVERY_RESTORE.md). Kokoro
#   requiredness comes from the explicit recovery policy/bundle, never
#   from E1's known historical-caller blind spot. Piper remains
#   station-owned: bundle, payload selection digest, and restored DB E1
#   model/config identity must match before publication -- see
#   monitoring/management/commands/provision_runtime_components.py's
#   _requirements_for_recovery_tts and isadoraair/runtime_recovery.py's
#   module docstring for why: a station can have BOTH features backed
#   by Kokoro live in production AND E1's own `kokoro.required` read as
#   false (webrequests/road_conditions' hardcoded KOKORO_BINARY callers
#   bypass StationTTSVoice entirely) -- re-deriving requiredness from
#   the freshly-restored database at this point would reintroduce
#   exactly that blind spot.
#
#   Explicit connected/fresh install (--legacy-connected-install, or no
#   --archive at all): UNCHANGED from Phase 4 -- ad hoc per-engine venv
#   + `pip install kokoro-onnx`/`pip install piper-tts`, optional
#   --kokoro-model-src/--piper-model-src, checksum/structural
#   verification, synthesis smoke test. A deliberate, separate,
#   operator-selected concern (task step 14), not a fallback a backup-
#   based restore ever reaches for on its own. --skip-kokoro/--skip-piper
#   only apply to this legacy path -- the recovery-payload path trusts
#   the payload's own declared component set as authoritative (an
#   operator who genuinely needs to skip a component that IS present in
#   the payload should use --legacy-connected-install instead of
#   silently ignoring their own --skip flag).
#
# Foundation E3's real apply() needs a Django environment to run as a
# manage.py command -- this stage therefore runs AFTER 60-python.sh in
# restore.sh's order, same as it always has. That command always comes
# from THIS checkout, never $RESTORE_TARGET_ROOT's own possibly-older
# copy -- only the Python interpreter running it comes from the restored
# target's venv, and only once verified compatible with this checkout's
# requirements.txt. See lib.sh's restore_manage / restore_manage.py's own
# docstring (Runtime Foundation E7C) for the full split.
#
# Usage:
#   deploy/restore/70-tts.sh --archive PATH [--plan|--apply]
#     [--staging-root PATH]
#   deploy/restore/70-tts.sh --legacy-connected-install [--plan|--apply]
#     [--staging-root PATH] [--skip-kokoro] [--skip-piper]
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
LEGACY_CONNECTED_INSTALL=0
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
    --legacy-connected-install) LEGACY_CONNECTED_INSTALL=1; shift ;;
    *) log_error "70-tts.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done

log_info "=== 70-tts (Kokoro + Piper) ==="
guard_production_target

USE_RECOVERY_PAYLOAD=0
if [ -n "$RESTORE_ARCHIVE" ] && [ "$LEGACY_CONNECTED_INSTALL" -eq 0 ]; then
  USE_RECOVERY_PAYLOAD=1
fi

if [ "$USE_RECOVERY_PAYLOAD" -eq 1 ]; then
  # =====================================================================
  # Backup-based disaster recovery: Runtime Foundation E7B payload path.
  # =====================================================================
  if [ "$SKIP_KOKORO" -eq 1 ] || [ "$SKIP_PIPER" -eq 1 ]; then
    log_error "--skip-kokoro/--skip-piper only apply to --legacy-connected-install -- the recovery-payload path publishes exactly what the payload declares. Re-run with --legacy-connected-install if selective skipping is genuinely needed."
    exit 2
  fi
  if [ -n "$KOKORO_MODEL_SRC" ] || [ -n "$PIPER_MODEL_SRC" ] || [ -n "$KOKORO_DIR" ] || [ -n "$PIPER_DIR" ]; then
    log_error "--kokoro-dir/--kokoro-model-src/--piper-dir/--piper-model-src only apply to --legacy-connected-install -- the recovery-payload path uses Foundation E3's own canonical/mapped target-root layout, never an ad hoc directory."
    exit 2
  fi
  require_cmd tar

  TTS_TARGET_ROOT="${RESTORE_STAGING_ROOT:-/}"
  log_info "TTS (E3) target root: $TTS_TARGET_ROOT"

  if [ "$RESTORE_MODE" != "apply" ]; then
    log_plan "locate + validate the runtime-recovery/ payload embedded in $RESTORE_ARCHIVE"
    log_plan "restore_manage provision_runtime_components --recovery-payload <payload>/tts --target-root $TTS_TARGET_ROOT --apply"
    log_info "70-tts: PLAN complete"
    exit 0
  fi

  # restore_manage (lib.sh) owns the venv-python and .env preconditions
  # (and whether that venv is even compatible with this checkout's
  # requirements.txt) with one shared, clear diagnostic -- this stage
  # only needs its own ordering precondition: has 20-application.sh
  # actually reconstructed the target checkout yet.
  if [ ! -f "$RESTORE_TARGET_ROOT/manage.py" ]; then
    log_error "$RESTORE_TARGET_ROOT/manage.py not found -- run 20-application.sh first."
    exit 1
  fi

  WORKDIR="$(mktemp -d /tmp/isadoraair-restore-tts-recovery.XXXXXX)"
  cleanup_tts_recovery() { rm -rf "$WORKDIR"; }
  trap cleanup_tts_recovery EXIT
  PAYLOAD_DIR="$WORKDIR/payload"

  restore_locate_recovery_payload "$PAYLOAD_DIR"
  if [ "$RESTORE_RECOVERY_PAYLOAD_FOUND" -ne 1 ]; then
    log_error "LEGACY ARCHIVE -- NOT SELF-CONTAINED FOR FOUNDATION E. Backup-based TTS recovery fails closed and never falls back to pip/PyPI. For an old archive, deliberately run the documented --legacy-connected-install path."
    exit 1
  fi

  log_apply "restore_manage validate_runtime_recovery_payload $PAYLOAD_DIR --json"
  RECOVERY_EVIDENCE_JSON=$(restore_manage validate_runtime_recovery_payload "$PAYLOAD_DIR" --json)

  if [ ! -d "$PAYLOAD_DIR/tts" ]; then
    log_warn "Recovery payload has no tts/ component -- not self-contained for TTS disaster recovery (this station's operator-established recovery policy did not include TTS in the prepared payload, or only native fdkaac was included). See docs/RUNTIME_BACKUP_PAYLOAD.md."
    log_info "70-tts: PASS (no TTS recovered -- see warning above)"
    exit 0
  fi

  log_apply "restore_manage provision_runtime_components --recovery-payload $PAYLOAD_DIR --target-root $TTS_TARGET_ROOT --apply"
  restore_manage provision_runtime_components \
      --recovery-payload "$PAYLOAD_DIR" \
      --target-root "$TTS_TARGET_ROOT" \
      --apply

  mapfile -t RECOVERED_TTS_COMPONENTS < <(
    python3 -c 'import json,sys; print("\n".join(json.loads(sys.argv[1]).get("tts_components", [])))' "$RECOVERY_EVIDENCE_JSON"
  )
  if [ "${#RECOVERED_TTS_COMPONENTS[@]}" -eq 0 ]; then
    log_error "validated TTS payload did not report any recoverable TTS components"
    exit 1
  fi
  restore_record_recovery_components "${RECOVERED_TTS_COMPONENTS[@]}" >/dev/null

  log_info "70-tts: PASS (TTS recovered from the Runtime Foundation E7 payload via Foundation E3's real provisioning authority)"
  exit 0
fi

# =========================================================================
# Legacy / explicit connected-install path -- UNCHANGED from Phase 4.
# =========================================================================
[ -z "$KOKORO_DIR" ] && KOKORO_DIR="${RESTORE_STAGING_ROOT:-$HOME}/kokoro"
[ -z "$PIPER_DIR" ] && PIPER_DIR="${RESTORE_STAGING_ROOT:-$HOME}/piper"

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
