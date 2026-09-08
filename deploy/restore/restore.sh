#!/usr/bin/env bash
# deploy/restore/restore.sh -- IsadoraAir 1.2 Phase 4.
#
# Orchestrator: runs every numbered stage (00 through 95) in order,
# passing the same flags through to each. Equivalent to running each
# deploy/restore/NN-*.sh script by hand in sequence -- this exists for
# convenience and to guarantee the order is never accidentally scrambled,
# not because the stages need shared state beyond what's already on disk
# at $RESTORE_TARGET_ROOT.
#
# Stops at the first stage that fails (no stage swallows another's
# failure) -- fix the reported problem and re-run; every stage is
# written to be safe to re-run (see each script's own idempotence note).
#
# Usage:
#   deploy/restore/restore.sh --archive PATH [--plan|--apply]
#     [--staging-root PATH] [--force-production-target] [--force-db] [--force-env]
#     [--resume [--adopt-pre-ledger]] [--non-interactive]
#     [--isa-user USER] [--isa-uid UID --isa-gid GID]
#     [-- <stage-specific args, passed to every stage that accepts them>]
#
# --resume (r0043): binds this run to a small durable ledger
# (/var/lib/isadoraair/restore/ledger.json -- see lib.sh's
# restore_ledger_* functions and restore_ledger.py) keyed on this exact
# --archive's SHA256 + the resolved target root. Stages that already
# durably completed against that exact identity verify their own output
# and converge (no-op) instead of re-doing expensive/destructive work
# (20-application.sh does not re-clone/re-extract .env;
# 30-postgresql.sh does not re-run pg_restore); a genuine ambiguity
# (ledger says done, filesystem/database disagrees) fails with a
# precise diagnostic, never silently either way. 80-companions.sh can
# also, ONLY with --resume and matching ledger provenance, repair the
# EXACT known legacy-WEATHER_DATA_DIR scaffold a pre-r0043 run could
# leave behind -- see docs/DISASTER_RECOVERY_STATUS.md's incident
# record. A different --archive against the same target root, or a
# corrupt/incomplete ledger, always fails closed. Without --resume,
# every stage behaves exactly as before r0043.
#
# --adopt-pre-ledger (r0044): the FIRST implementation step for restores
# that BEGAN before the ledger existed at all (e.g. an interrupted
# pre-r0043 restore) -- combined with --resume, each stage independently
# VERIFIES its own durable output against the supplied archive (the SAME
# verification --resume's own "ledger already says complete" branch
# uses) and, only if that verification passes, adopts it: records the
# stage complete in a freshly-created ledger without redoing the
# underlying work. Completion is NEVER inferred merely because files
# exist -- see each stage's own "Adopt" section and
# docs/DISASTER_RECOVERY_STATUS.md's "Resumable restore mechanism"
# section for the exact per-stage evidence required. A different
# archive, or state that fails verification, fails closed with a
# precise diagnostic -- adoption never falls back to a guess.
#
# --non-interactive: skip the interactive detect-and-prompt preflight
# below even when a real TTY is attached (stdin is not a TTY at all --
# e.g. piped/redirected input, common for CI -- already skips it
# automatically). Deterministic automation/tests should prefer passing
# --resume/--adopt-pre-ledger explicitly rather than relying on the
# interactive prompt at all.
#
# ## Interactive workflow (r0044)
#
# An operator should not need to remember --resume/--adopt-pre-ledger/
# --force-env/--force-db/stage numbers. When this orchestrator runs
# --apply, with a real TTY on both stdin and stdout, without
# --non-interactive, and without --resume/--adopt-pre-ledger already
# given explicitly, it inspects the target root and any existing ledger
# BEFORE doing anything, and presents one of:
#
#   - A ledger already exists and matches this exact archive/target:
#     [R] Resume recovery / [V] Verify completed stages (run each
#     already-complete stage's own verification, then stop -- nothing
#     beyond the last completed stage is touched) / [S] Show recovery
#     status (read-only) / [Q] Quit (no changes).
#   - No ledger exists, but the target already shows restore state
#     (a .git checkout and/or a non-empty .env -- e.g. an interrupted
#     pre-r0043 restore): [A] Verify and adopt this interrupted recovery
#     (adds --resume --adopt-pre-ledger) / [N] Treat this as a new
#     recovery (proceeds exactly as before r0043/r0044 -- existing
#     content still requires --force-env/--force-db if it turns out to
#     be real) / [S] Show detected state / [Q] Quit.
#   - A ledger exists but belongs to a DIFFERENT archive or target root,
#     or an existing ledger is corrupt/schema-invalid: always a hard,
#     immediate failure -- never a menu, never silently ignored.
#   - No ledger and no detected pre-existing state at all: proceeds
#     immediately, nothing to ask about.
#
# The prompt is never itself authorization to weaken any safety check --
# every choice above still runs through the exact same fail-closed
# verification each stage script already implements.
#
# --isa-user/--isa-uid/--isa-gid are the one exception to "every stage
# gets the same args": they are routed ONLY to 90-system-config.sh and
# 95-validate.sh (the only stages that recognize them) -- see this
# script's own identity-flag routing below.
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
# Identity flags (--isa-user/--isa-uid/--isa-gid) are recognized ONLY by
# 90-system-config.sh and 95-validate.sh -- every other stage's own
# strict "unrecognized argument" guard would reject them if broadcast
# via the same args every stage otherwise receives identically. Runtime
# Foundation E7D (2026-09-04): pulled out here and forwarded ONLY to
# those two stages, so a full end-to-end restore.sh run can supply a
# trusted --isa-uid/--isa-gid pair (e.g. for an isolated --staging-root
# target with no /etc/passwd of its own -- see 90-system-config.sh's and
# 95-validate.sh's own headers) without breaking every earlier stage.
# Deliberately narrow: no other stage-specific flag gets this special-
# cased routing, and no other stage's own argument surface is touched.
#
# --non-interactive is ALSO stripped here (r0044) -- it is meaningful
# only to this orchestrator's own preflight below, no individual stage
# script recognizes it.
COMMON_ARGS=()
IDENTITY_ARGS=()
NON_INTERACTIVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --isa-user|--isa-uid|--isa-gid)
      IDENTITY_ARGS+=("$1" "${2:?$1 needs a value}"); shift 2 ;;
    --isa-user=*|--isa-uid=*|--isa-gid=*)
      IDENTITY_ARGS+=("$1"); shift ;;
    --non-interactive)
      NON_INTERACTIVE=1; shift ;;
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

# ---------------------------------------------------------------------
# Interactive preflight (r0044) -- see this file's own header for the
# full menu contract. Skipped entirely (falls straight through to the
# stage loop, unchanged from pre-r0044 behavior) unless ALL of:
#   - --apply (a --plan run never writes, nothing to ask about);
#   - a real TTY on both stdin and stdout;
#   - --non-interactive was not given;
#   - --resume was not already given explicitly (an operator who already
#     knows to pass --resume has already made their choice).
# ---------------------------------------------------------------------
_restore_interactive_read_choice() {
  local prompt="$1" default="${2:-}" reply=""
  if [ -r /dev/tty ]; then
    read -r -p "$prompt" reply < /dev/tty || true
  else
    read -r -p "$prompt" reply || true
  fi
  printf '%s\n' "${reply:-$default}"
}

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
    case "$stage" in
      90-system-config.sh|95-validate.sh)
        "$SCRIPT_DIR/$stage" "${COMMON_ARGS[@]}" --resume "${IDENTITY_ARGS[@]}" ;;
      *)
        "$SCRIPT_DIR/$stage" "${COMMON_ARGS[@]}" --resume ;;
    esac
  done
  echo
  echo "=== All ledger-recorded stages verified. ==="
}

if [ "$RESTORE_MODE" = "apply" ] && [ "$NON_INTERACTIVE" -eq 0 ] \
    && [ -t 0 ] && [ -t 1 ] && [ "$RESTORE_RESUME" -ne 1 ] \
    && [ -n "$RESTORE_ARCHIVE" ] && [ -f "$RESTORE_ARCHIVE" ]; then
  _restore_interactive_preflight
fi

echo "=== IsadoraAir restore orchestrator: ${#STAGES[@]} stages ==="
for stage in "${STAGES[@]}"; do
  echo
  echo ">>> Running $stage"
  case "$stage" in
    90-system-config.sh|95-validate.sh)
      "$SCRIPT_DIR/$stage" "${COMMON_ARGS[@]}" "${IDENTITY_ARGS[@]}" ;;
    *)
      "$SCRIPT_DIR/$stage" "${COMMON_ARGS[@]}" ;;
  esac
done
echo
echo "=== All stages completed. ==="
echo "Nothing was started/enabled/reloaded -- see deploy/restore/README.md's"
echo "'Restore-order dependency map' and docs/DISASTER_RECOVERY_RESTORE.md's"
echo "'Service bring-up order' section for what comes next (Phase 5)."
