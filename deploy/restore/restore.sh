#!/usr/bin/env bash
# deploy/restore/restore.sh -- IsadoraAir 1.2 Phase 4.
#
# Orchestrator: runs every numbered stage (00 through 95) in order.
# Equivalent to running each deploy/restore/NN-*.sh script by hand in
# sequence -- this exists for convenience and to guarantee the order is
# never accidentally scrambled, not because the stages need shared
# state beyond what's already on disk at $RESTORE_TARGET_ROOT.
#
# Stops at the first stage that fails (no stage swallows another's
# failure) -- fix the reported problem and re-run; every stage is
# written to be safe to re-run (see each script's own idempotence note).
#
# Usage:
#   deploy/restore/restore.sh --archive PATH [--plan|--apply]
#     [--staging-root PATH] [--force-production-target] [--force-db] [--force-env]
#     [--resume [--adopt-pre-ledger]] [--non-interactive]
#     [--recovery-media-root PATH] [--owner USER:GROUP]
#     [--isa-user USER] [--isa-uid UID --isa-gid GID]
#
# Common arguments (--staging-root, --force-*, --resume, ...) are
# broadcast identically to every stage, exactly as before. A handful of
# genuinely stage-specific concerns are NOT broadcast -- each is routed
# only to the stage(s) that actually recognize it (r0045 generalizes
# this from the identity-flag routing r0040 already established):
#
#   --isa-user/--isa-uid/--isa-gid  -> 90-system-config.sh, 95-validate.sh
#   --owner USER:GROUP              -> 20-application.sh, 40-station-content.sh
#   (recovery-media-derived args)   -> see "Recovery media" below
#
# --resume (r0043): binds this run to a small durable ledger
# (/var/lib/isadoraair/restore/ledger.json -- see lib.sh's
# restore_ledger_* functions and restore_ledger.py) keyed on this exact
# --archive's SHA256 + the resolved target root. Stages that already
# durably completed against that exact identity verify their own output
# and converge (no-op) instead of re-doing expensive/destructive work; a
# genuine ambiguity (ledger says done, filesystem/database disagrees)
# fails with a precise diagnostic, never silently either way. Without
# --resume, every stage behaves exactly as before r0043.
#
# --adopt-pre-ledger (r0044): for restores that BEGAN before the ledger
# existed at all -- combined with --resume, each stage independently
# VERIFIES its own durable output against the supplied archive and,
# only if that verification passes, adopts it into a freshly-created
# ledger without redoing the underlying work. A different archive, or
# state that fails verification, fails closed -- adoption never falls
# back to a guess. See docs/DISASTER_RECOVERY_STATUS.md's "Resumable
# restore mechanism" section for the exact per-stage evidence required.
#
# --non-interactive: skip the interactive preflight below (media
# discovery AND the ledger/adopt menu) even when a real TTY is attached
# (stdin not being a TTY at all -- piped/redirected, the normal CI
# shape -- already skips both automatically). Deterministic automation/
# tests should prefer passing --resume/--adopt-pre-ledger/
# --recovery-media-root explicitly rather than relying on either
# interactive step.
#
# ## Recovery media (r0045)
#
# A full offline (E8) restore needs several stage-specific frozen
# inputs -- Stage 10's local apt/snap closures, Stage 20's local Git
# mirror, Stage 60/80's offline pip wheelhouse, Stage 80's local
# companion Git mirrors -- that used to require operator-supplied,
# stage-specific flags (--apt-repo-dir, --snap-dir, --repo-url,
# --repo-url-prefix, --with-*, PIP_* env vars) with no single top-level
# concept tying them together. `--recovery-media-root PATH` (or
# interactive discovery -- see below) now names ONE coherent directory
# tree (see recovery_media.py's own module docstring for the exact
# layout contract, established directly from
# deploy/restore/build_offline_closure.py's own --out-dir structure and
# the E8 export procedure -- never guessed) that this script validates
# once, then derives every one of those stage-specific inputs from
# internally -- an operator never types any of them.
#
# Interactively (real TTY, --apply, no --non-interactive, no
# --recovery-media-root already given): discovers candidate recovery-
# media roots near the supplied --archive (and under $HOME). Exactly
# one archive-matching candidate is used automatically (shown, not
# silently); an ambiguous set is listed for the operator to choose
# from (or decline, if none are actually wanted); nothing found prompts
# for a path, blank means "no recovery media -- online/default sources
# for stages that need them." Non-interactively, only --recovery-media-
# root is consulted -- discovery/prompting never happens.
#
# --isa-user/--isa-uid/--isa-gid are the one exception to "every stage
# gets the same args" r0040 already established -- routed ONLY to
# 90-system-config.sh and 95-validate.sh; see below.
#
# For a real Phase 5 bare-machine drill, prefer running stages
# individually and reviewing each one's output before proceeding to the
# next, rather than this orchestrator's fire-and-forget sequence --
# --plan end-to-end first is strongly recommended regardless.
#
# NOTE on chained --plan runs: --plan never writes anything, so a stage
# that depends on an EARLIER stage's real output (e.g. 30-postgresql.sh
# reading DB credentials from the .env that 20-application.sh would have
# restored) will correctly report that dependency as missing and stop --
# this is expected, not a bug in the orchestrator. A full, meaningful
# preview of the whole chain requires --staging-root --apply (isolated,
# safe to run for real) rather than --plan alone.
#
# Runtime Foundation E7B (2026-08-29): 60-python now runs BEFORE
# 50-native-deps, reversing their numeric order. This is deliberate,
# not a typo -- backup-based disaster recovery's native fdkaac now
# delegates to Foundation E4's real prepare/publish authority via
# `manage.py provision_runtime_components` (see 50-native-deps.sh's own
# header), which needs the restored app's Python environment
# (60-python.sh's job) to even run. 60-python.sh is safe to run early:
# it only needs 20-application.sh (manage.py + requirements.txt) and is
# idempotent (verifies rather than recreates if the venv already
# exists), so running it here costs nothing when it's reached again at
# its usual numeric spot. See deploy/restore/README.md's "Restore-order
# dependency map" for the full picture -- the file/stage NUMBERS stay
# as stable identifiers (`ls` sort order, individual invocation), they
# no longer imply a strict execution order on their own.
#
# r0030: 75-protected-updater.sh added, placed after 70-tts.sh (same
# app-source/venv prerequisite, no DB/nginx/companion dependency) and
# before 80-companions.sh -- restores the Phase-D protected updater
# component from an embedded runtime-recovery payload, the same
# locate/validate/publish/record-receipt shape 50/70 already use for
# their own components. See deploy/restore/README.md's dependency map.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

STAGES=(
  00-preflight.sh
  10-packages.sh
  20-application.sh
  30-postgresql.sh
  40-station-content.sh
  60-python.sh
  50-native-deps.sh
  70-tts.sh
  75-protected-updater.sh
  80-companions.sh
  90-system-config.sh
  95-validate.sh
)

# ---------------------------------------------------------------------
# Flag routing. Identity flags (--isa-user/--isa-uid/--isa-gid) and
# --owner are pulled out here and forwarded ONLY to the stage(s) that
# actually recognize them -- every other stage's own strict
# "unrecognized argument" guard would reject them if broadcast via the
# same args every stage otherwise receives identically. --non-
# interactive and --recovery-media-root are ALSO consumed here -- both
# are meaningful only to this orchestrator's own preflight below, no
# individual stage script recognizes either.
COMMON_ARGS=()
IDENTITY_ARGS=()
OWNER_ARGS=()
NON_INTERACTIVE=0
EXPLICIT_MEDIA_ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --isa-user|--isa-uid|--isa-gid)
      IDENTITY_ARGS+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
    --isa-user=*|--isa-uid=*|--isa-gid=*)
      IDENTITY_ARGS+=("$1"); shift ;;
    --owner)
      OWNER_ARGS=(--owner "${2:?--owner needs USER:GROUP}"); shift 2 ;;
    --owner=*)
      OWNER_ARGS=(--owner "${1#*=}"); shift ;;
    --non-interactive)
      NON_INTERACTIVE=1; shift ;;
    --recovery-media-root)
      EXPLICIT_MEDIA_ROOT="${2:?--recovery-media-root needs a path}"; shift 2 ;;
    --recovery-media-root=*)
      EXPLICIT_MEDIA_ROOT="${1#*=}"; shift ;;
    *)
      COMMON_ARGS+=("$1"); shift ;;
  esac
done

# Resolve mode/archive/target-root/resume/adopt from COMMON_ARGS (exactly
# what every stage script will independently re-resolve from the same
# argv) so this orchestrator's own interactive preflight can reason
# about them -- never mutates COMMON_ARGS itself; each stage still does
# its own, identical parsing later.
restore_parse_common_args "${COMMON_ARGS[@]}"

# An explicit --resume means the operator has already told us exactly
# what to do -- never prompt for anything (media root included), matching
# the pre-r0045 contract that --resume runs fully unattended.
INTERACTIVE_ELIGIBLE=0
if [ "$RESTORE_MODE" = "apply" ] && [ "$NON_INTERACTIVE" -eq 0 ] \
    && [ -t 0 ] && [ -t 1 ] && [ "$RESTORE_RESUME" -ne 1 ] \
    && [ -n "$RESTORE_ARCHIVE" ] && [ -f "$RESTORE_ARCHIVE" ]; then
  INTERACTIVE_ELIGIBLE=1
fi

_restore_interactive_read_choice() {
  local prompt="$1" default="${2:-}" reply=""
  if [ -r /dev/tty ]; then
    read -r -p "$prompt" reply < /dev/tty || true
  else
    read -r -p "$prompt" reply || true
  fi
  printf '%s\n' "${reply:-$default}"
}

# ---------------------------------------------------------------------
# Recovery media (r0045) -- see recovery_media.py's own module
# docstring and this file's own header for the full layout contract and
# UX. Populates MEDIA_ROOT (possibly empty -- "no recovery media, use
# online/default sources") and the per-stage arg/env arrays below.
# ---------------------------------------------------------------------
MEDIA_ROOT=""
STAGE10_ARGS=()
STAGE20_ARGS=()
STAGE60_ENV=()
STAGE80_ARGS=()
STAGE80_ENV=()

_restore_show_media_evidence() {
  local evidence_json="$1"
  python3 -c '
import json, sys
e = json.loads(sys.argv[1])
print("  Root: " + e["root"])
print("  Archive present in backups/: " + str(e["archive_match"]))
print("  IsadoraAir.git mirror: " + e["isadoraair_git"])
print("  Companion mirrors: " + (", ".join(e["companion_repo_names"]) or "(none)"))
print("  apt-repo: " + e["apt_repo_dir"])
print("  snaps: " + e["snap_dir"])
print("  wheelhouse: " + e["wheelhouse_dir"])
' "$evidence_json"
}

_restore_resolve_media_root() {
  if [ -n "$EXPLICIT_MEDIA_ROOT" ]; then
    local evidence
    if ! evidence=$(restore_media_validate "$EXPLICIT_MEDIA_ROOT" "$RESTORE_ARCHIVE"); then
      log_error "--recovery-media-root $EXPLICIT_MEDIA_ROOT is not a valid/complete recovery-media tree:"
      python3 -c 'import json,sys; e=json.loads(sys.argv[1]); [print("  - " + p) for p in e["problems"] + e.get("notes", [])]' "$evidence" >&2
      exit 1
    fi
    MEDIA_ROOT="$EXPLICIT_MEDIA_ROOT"
    log_info "Recovery media: using explicitly-supplied $MEDIA_ROOT"
    return 0
  fi

  if [ "$INTERACTIVE_ELIGIBLE" -ne 1 ]; then
    return 0  # non-interactive, no explicit root -- no media routing, exactly pre-r0045 behavior
  fi

  local candidates archive_dir
  archive_dir=$(cd "$(dirname "$RESTORE_ARCHIVE")" && pwd)
  candidates=$(restore_media_discover "$RESTORE_ARCHIVE" "$archive_dir" "$HOME") || candidates="[]"
  local count matching_count
  count=$(python3 -c 'import json,sys; print(len(json.loads(sys.argv[1])))' "$candidates")
  matching_count=$(python3 -c 'import json,sys; print(sum(1 for c in json.loads(sys.argv[1]) if c["archive_match"]))' "$candidates")

  if [ "$matching_count" -eq 1 ]; then
    local only
    only=$(python3 -c 'import json,sys; print(json.dumps(next(c for c in json.loads(sys.argv[1]) if c["archive_match"])))' "$candidates")
    echo
    echo "Recovery media found for this archive:"
    _restore_show_media_evidence "$only"
    MEDIA_ROOT=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["root"])' "$only")
    return 0
  fi

  if [ "$count" -eq 0 ]; then
    echo
    local reply
    reply=$(_restore_interactive_read_choice "No local recovery media (frozen apt/snap/wheelhouse/Git-mirror closure) was found for this archive.
If this is an offline (E8-style) restore, enter its recovery-media root now.
Leave blank to proceed with online/default sources for any stage that needs them.
Recovery-media root []: " "")
    if [ -n "$reply" ]; then
      local evidence
      if ! evidence=$(restore_media_validate "$reply" "$RESTORE_ARCHIVE"); then
        log_error "$reply is not a valid/complete recovery-media tree:"
        python3 -c 'import json,sys; e=json.loads(sys.argv[1]); [print("  - " + p) for p in e["problems"] + e.get("notes", [])]' "$evidence" >&2
        exit 1
      fi
      MEDIA_ROOT="$reply"
      log_info "Recovery media: using operator-supplied $MEDIA_ROOT"
    fi
    return 0
  fi

  # Ambiguous: more than one archive-matching candidate, or some
  # structurally-valid candidates but none actually contain this exact
  # archive -- shown honestly either way, never guessed.
  echo
  echo "Multiple possible recovery-media roots were found; none is an unambiguous single match for this archive:"
  local i=0 roots=() line
  while IFS=$'\t' read -r root match; do
    i=$((i + 1))
    roots+=("$root")
    echo "  [$i] $root (archive present: $match)"
  done < <(python3 -c 'import json,sys
for c in json.loads(sys.argv[1]):
    print(c["root"] + "\t" + str(c["archive_match"]))' "$candidates")
  echo "  [0] None of these -- proceed with online/default sources"
  local choice
  choice=$(_restore_interactive_read_choice "Choice [0]: " "0")
  if [ "$choice" = "0" ] || [ -z "$choice" ]; then
    log_info "Recovery media: none selected -- proceeding with online/default sources."
    return 0
  fi
  if ! [[ "$choice" =~ ^[0-9]+$ ]] || [ "$choice" -lt 1 ] || [ "$choice" -gt "${#roots[@]}" ]; then
    log_error "Invalid selection: $choice"
    exit 1
  fi
  MEDIA_ROOT="${roots[$((choice - 1))]}"
  log_info "Recovery media: using $MEDIA_ROOT"
}

_restore_build_media_stage_args() {
  [ -z "$MEDIA_ROOT" ] && return 0

  local groups_json
  groups_json=$(restore_media_detect_apt_groups "$MEDIA_ROOT")
  local with_cd_rip with_kokoro with_selenium with_backup_enc
  with_cd_rip=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["OPTIONAL_CD_RIP"])' "$groups_json")
  with_kokoro=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["OPTIONAL_KOKORO_TTS"])' "$groups_json")
  with_selenium=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["OPTIONAL_SYNDICATED_SELENIUM"])' "$groups_json")
  with_backup_enc=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["OPTIONAL_BACKUP_ENCRYPTION"])' "$groups_json")

  STAGE10_ARGS=(--apt-repo-dir "$MEDIA_ROOT/offline/apt-repo" --snap-dir "$MEDIA_ROOT/offline/snaps")
  [ "$with_cd_rip" = "True" ] && STAGE10_ARGS+=(--with-cd-rip)
  [ "$with_kokoro" = "True" ] && STAGE10_ARGS+=(--with-kokoro-tts)
  [ "$with_selenium" = "True" ] && STAGE10_ARGS+=(--with-syndicated-selenium)
  [ "$with_backup_enc" = "True" ] && STAGE10_ARGS+=(--with-backup-encryption)
  # HE-AAC (BUILD_HEAAC) is included by default and never skipped here --
  # --skip-heaac-build is deliberately never added to STAGE10_ARGS.

  STAGE20_ARGS=(--repo-url "file://$MEDIA_ROOT/repos/IsadoraAir.git")
  STAGE80_ARGS=(--repo-url-prefix "file://$MEDIA_ROOT/repos")

  local wheelhouse="$MEDIA_ROOT/offline/wheelhouse"
  STAGE60_ENV=(
    "PIP_NO_INDEX=1"
    "PIP_FIND_LINKS=$wheelhouse"
    "PIP_DISABLE_PIP_VERSION_CHECK=1"
  )
  STAGE80_ENV=("${STAGE60_ENV[@]}")

  log_info "Recovery media: Stage 10 offline apt/snap closure + $(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print(",".join(k for k,v in d.items() if v) or "no optional groups")' "$groups_json")"
  log_info "Recovery media: Stage 20/80 local Git mirrors at $MEDIA_ROOT/repos"
  log_info "Recovery media: Stage 60/80 pip constrained offline to $wheelhouse (PIP_NO_INDEX=1)"
}

# _restore_run_stage STAGE EXTRA_RESUME_FLAG -- the one place every
# stage invocation happens (main loop AND the verify-only path below),
# so per-stage argument/env routing is defined exactly once.
_restore_run_stage() {
  local stage="$1" extra_resume="${2:-}"
  case "$stage" in
    90-system-config.sh|95-validate.sh)
      "$SCRIPT_DIR/$stage" "${COMMON_ARGS[@]}" "${IDENTITY_ARGS[@]}" $extra_resume ;;
    10-packages.sh)
      "$SCRIPT_DIR/$stage" "${COMMON_ARGS[@]}" "${STAGE10_ARGS[@]}" $extra_resume ;;
    20-application.sh)
      "$SCRIPT_DIR/$stage" "${COMMON_ARGS[@]}" "${STAGE20_ARGS[@]}" "${OWNER_ARGS[@]}" $extra_resume ;;
    40-station-content.sh)
      "$SCRIPT_DIR/$stage" "${COMMON_ARGS[@]}" "${OWNER_ARGS[@]}" $extra_resume ;;
    60-python.sh)
      env "${STAGE60_ENV[@]}" "$SCRIPT_DIR/$stage" "${COMMON_ARGS[@]}" $extra_resume ;;
    80-companions.sh)
      env "${STAGE80_ENV[@]}" "$SCRIPT_DIR/$stage" "${COMMON_ARGS[@]}" "${STAGE80_ARGS[@]}" $extra_resume ;;
    *)
      "$SCRIPT_DIR/$stage" "${COMMON_ARGS[@]}" $extra_resume ;;
  esac
}

# ---------------------------------------------------------------------
# Ledger/adoption preflight (r0044) -- see this file's own header for
# the full menu contract.
# ---------------------------------------------------------------------
_restore_interactive_preflight() {
  local ledger_path archive_sha256 describe_json
  ledger_path="$(restore_ledger_path)"
  archive_sha256="$(sha256sum "$RESTORE_ARCHIVE" | cut -d' ' -f1)"

  # A corrupt/schema-invalid existing ledger fails closed immediately --
  # never silently treated as "no ledger" just because reading it failed.
  if ! describe_json=$(python3 "$SCRIPT_DIR/restore_ledger.py" describe --ledger "$ledger_path" 2>&1); then
    log_error "Existing restore ledger at $ledger_path is corrupt or invalid -- refusing to proceed automatically: $describe_json"
    exit 1
  fi

  local present
  present=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("present", False))' "$describe_json")

  if [ "$present" = "True" ]; then
    local ledger_archive ledger_target
    ledger_archive=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("archive_sha256",""))' "$describe_json")
    ledger_target=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("target_root",""))' "$describe_json")
    if [ "$ledger_archive" != "$archive_sha256" ] || [ "$ledger_target" != "$RESTORE_TARGET_ROOT" ]; then
      log_error "A restore ledger already exists at $ledger_path for a DIFFERENT archive and/or target root."
      log_error "  Ledger archive SHA256: $ledger_archive   This run's archive SHA256: $archive_sha256"
      log_error "  Ledger target root:    $ledger_target   This run's target root:    $RESTORE_TARGET_ROOT"
      log_error "A different archive must never silently inherit a prior restore session. Remove the stale ledger explicitly first if this is genuinely intentional."
      exit 1
    fi
    echo
    echo "Existing IsadoraAir recovery session found."
    echo "Archive SHA256: $archive_sha256"
    echo "Target: $RESTORE_TARGET_ROOT"
    local last_complete last_incomplete stage_name
    last_complete=""
    last_incomplete=""
    for stage in "${STAGES[@]}"; do
      stage_name="${stage%.sh}"
      local state
      state=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("stages",{}).get(sys.argv[2],{}).get("state","absent"))' "$describe_json" "$stage_name")
      if [ "$state" = "complete" ]; then
        last_complete="$stage_name"
      elif [ -z "$last_incomplete" ]; then
        last_incomplete="$stage_name"
      fi
    done
    echo "Last completed stage: ${last_complete:-<none>}"
    echo "Last failed/incomplete stage: ${last_incomplete:-<none -- all stages already complete>}"
    while true; do
      local choice
      choice=$(_restore_interactive_read_choice "[R] Resume recovery  [V] Verify completed stages  [S] Show recovery status  [Q] Quit
Choice [R]: " "R")
      case "$choice" in
        R|r|Resume|resume)
          COMMON_ARGS+=(--resume)
          return 0
          ;;
        V|v|Verify|verify)
          _restore_interactive_verify_only "$describe_json"
          exit 0
          ;;
        S|s|Show|show)
          echo "$describe_json" | python3 -m json.tool
          ;;
        Q|q|Quit|quit)
          echo "Cancelled -- no changes made."
          exit 0
          ;;
        *)
          echo "Please choose R, V, S, or Q."
          ;;
      esac
    done
  fi

  # No ledger at all -- detect pre-ledger restore evidence. Deliberately
  # cheap/minimal (a .git checkout and/or a non-empty .env): the REAL
  # verification, for whichever stage actually needs it, always happens
  # inside that stage's own adoption logic regardless of what this menu
  # shows -- this detection only decides whether to ask at all.
  local has_git=0 has_env=0
  [ -d "$RESTORE_TARGET_ROOT/.git" ] && has_git=1
  [ -s "$RESTORE_TARGET_ROOT/.env" ] && has_env=1
  if [ "$has_git" -eq 0 ] && [ "$has_env" -eq 0 ]; then
    return 0  # genuinely fresh target -- nothing to ask about
  fi

  echo
  echo "Existing IsadoraAir restore state was found, but no recovery ledger exists."
  echo "This may be an interrupted restore created by older IsadoraAir recovery tooling."
  echo "Supplied backup:"
  echo "  SHA256: $archive_sha256"
  while true; do
    local choice
    choice=$(_restore_interactive_read_choice "[A] Verify and adopt this interrupted recovery  [N] Treat this as a new recovery  [S] Show detected state  [Q] Quit
Choice: " "")
    case "$choice" in
      A|a|Adopt|adopt)
        COMMON_ARGS+=(--resume --adopt-pre-ledger)
        return 0
        ;;
      N|n|New|new)
        return 0  # proceeds exactly as before r0043/r0044
        ;;
      S|s|Show|show)
        echo "Detected at $RESTORE_TARGET_ROOT: .git checkout=$([ "$has_git" -eq 1 ] && echo yes || echo no), non-empty .env=$([ "$has_env" -eq 1 ] && echo yes || echo no)"
        ;;
      Q|q|Quit|quit)
        echo "Cancelled -- no changes made."
        exit 0
        ;;
      *)
        echo "Please choose A, N, S, or Q."
        ;;
    esac
  done
}

# _restore_interactive_verify_only DESCRIBE_JSON -- runs ONLY the
# stages the ledger already records complete, each with --resume (so
# each independently re-verifies its own durable output rather than
# trusting the ledger alone -- exactly today's --resume contract), and
# stops at the first stage the ledger does NOT record complete, without
# ever running it. Purely a status/integrity check -- never advances
# the restore itself.
_restore_interactive_verify_only() {
  local describe_json="$1" stage stage_name state
  echo
  echo "=== Verifying completed stages only (nothing beyond will be run) ==="
  for stage in "${STAGES[@]}"; do
    stage_name="${stage%.sh}"
    state=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1]).get("stages",{}).get(sys.argv[2],{}).get("state","absent"))' "$describe_json" "$stage_name")
    if [ "$state" != "complete" ]; then
      echo "Stopping: $stage_name is not yet recorded complete."
      return 0
    fi
    echo
    echo ">>> Verifying $stage"
    _restore_run_stage "$stage" "--resume"
  done
  echo
  echo "=== All ledger-recorded stages verified. ==="
}

# ---------------------------------------------------------------------
# Main body -- guarded so a test can `source` this file (BASH_SOURCE !=
# $0, e.g. from a test harness's own bash process) to reach every
# function/variable defined above for isolated, no-side-effect testing
# (e.g. calling _restore_build_media_stage_args directly after setting
# MEDIA_ROOT by hand) without ALSO running the real preflight/stage
# loop. A normal `bash restore.sh ...` / `./restore.sh ...` invocation
# is completely unaffected -- BASH_SOURCE[0] == $0 in that case, exactly
# as it always has been.
# ---------------------------------------------------------------------
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  # _restore_resolve_media_root itself handles every case: an explicit
  # --recovery-media-root is validated regardless of interactive
  # eligibility (the deterministic automation/CI path); otherwise it's a
  # no-op unless this run is actually interactive-eligible.
  _restore_resolve_media_root
  _restore_build_media_stage_args

  if [ "$INTERACTIVE_ELIGIBLE" -eq 1 ]; then
    _restore_interactive_preflight
  fi

  echo "=== IsadoraAir restore orchestrator: ${#STAGES[@]} stages ==="
  for stage in "${STAGES[@]}"; do
    echo
    echo ">>> Running $stage"
    _restore_run_stage "$stage"
  done
  echo
  echo "=== All stages completed. ==="
  echo "Nothing was started/enabled/reloaded -- see deploy/restore/README.md's"
  echo "'Restore-order dependency map' and docs/DISASTER_RECOVERY_RESTORE.md's"
  echo "'Service bring-up order' section for what comes next (Phase 5)."
fi
