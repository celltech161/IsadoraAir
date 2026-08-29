#!/usr/bin/env bash
# Shared helpers for every deploy/restore/*.sh stage -- IsadoraAir 1.2
# Phase 4. Sourced (not executed directly) by every NN-*.sh stage script
# plus inspect_backup.sh. Establishes the plan/apply/staging safety
# model every stage script obeys, so the safety boundary is enforced in
# ONE place rather than re-implemented (and potentially forgotten) in
# each of the ten-plus stage scripts.
#
# ## The three modes (see deploy/restore/README.md for the full writeup)
#
#   --plan (default)      Never writes anything. Every stage prints what
#                          it WOULD do and exits 0. Safe to run against
#                          production at any time, including this very
#                          box while it's live on-air -- this is how
#                          Phase 4's own staging validation exercised
#                          each stage's logic without touching anything.
#   --apply                Actually performs the stage's writes. Refuses
#                          outright (see guard_production_target below)
#                          if the resolved target root is the box's own
#                          live IsadoraAir install, unless the operator
#                          also passes --force-production-target -- a
#                          second, deliberate flag, not a typo away from
#                          --apply alone.
#   --staging-root PATH    Redirects every stage's target root under
#                          PATH instead of the real canonical location
#                          (default /opt/isadoraair) -- e.g.
#                          --staging-root /tmp/isadoraair-restore-test
#                          --apply lets every stage's write path actually
#                          execute, safely, against a throwaway tree.
#                          This is how Phase 4 exercised the restore
#                          machinery for real (Section 39) without ever
#                          pointing at production.
#
# --plan and --staging-root --apply are the two modes Phase 4 actually
# ran. Bare --apply (no --staging-root) against this box is meant for
# Phase 5's clean-machine drill, not for exercising Phase 4 itself.
set -euo pipefail

# ---------------------------------------------------------------------
# Logging -- plain, timestamped, no color codes (these scripts are as
# likely to be read from a journalctl/redirected-file transcript during
# a real outage as from an interactive terminal).
# ---------------------------------------------------------------------
_restore_ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log_info()  { printf '[%s] [INFO]  %s\n' "$(_restore_ts)" "$*"; }
log_warn()  { printf '[%s] [WARN]  %s\n' "$(_restore_ts)" "$*" >&2; }
log_error() { printf '[%s] [ERROR] %s\n' "$(_restore_ts)" "$*" >&2; }
# log_plan: what a --plan run would do, had it been --apply. Distinct
# from log_info so `grep '\[PLAN\]'` on a run's output is a complete,
# reliable summary of every write the run would have performed.
log_plan()  { printf '[%s] [PLAN]  %s\n' "$(_restore_ts)" "$*"; }
# log_apply: an actual write about to happen, --apply mode only. Always
# paired with the log_plan message that would fire in --plan for the
# same action, so the two modes' output is directly diffable.
log_apply() { printf '[%s] [APPLY] %s\n' "$(_restore_ts)" "$*"; }

# Never call this with a secret value -- see deploy/restore/README.md's
# "Logging" section. Reminder only; nothing here can enforce it.
redact() { printf '<redacted, %d bytes>' "${#1}"; }

# ---------------------------------------------------------------------
# Common flag parsing. Each stage script does:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
#   restore_parse_common_args "$@"
#   set -- "${RESTORE_REMAINING_ARGS[@]}"   # stage-specific flags, if any
# ---------------------------------------------------------------------
RESTORE_MODE="plan"
RESTORE_STAGING_ROOT=""
RESTORE_TARGET_ROOT=""
RESTORE_FORCE_PRODUCTION_TARGET=0
RESTORE_FORCE_DB=0
RESTORE_FORCE_ENV=0
RESTORE_DB_NAME=""
RESTORE_ARCHIVE=""
RESTORE_REMAINING_ARGS=()

restore_parse_common_args() {
  RESTORE_REMAINING_ARGS=()
  while [ $# -gt 0 ]; do
    case "$1" in
      --plan) RESTORE_MODE="plan"; shift ;;
      --apply) RESTORE_MODE="apply"; shift ;;
      --staging-root)
        RESTORE_STAGING_ROOT="${2:?--staging-root needs a path}"; shift 2 ;;
      --staging-root=*) RESTORE_STAGING_ROOT="${1#*=}"; shift ;;
      --target-root)
        RESTORE_TARGET_ROOT="${2:?--target-root needs a path}"; shift 2 ;;
      --target-root=*) RESTORE_TARGET_ROOT="${1#*=}"; shift ;;
      --archive)
        RESTORE_ARCHIVE="${2:?--archive needs a path}"; shift 2 ;;
      --archive=*) RESTORE_ARCHIVE="${1#*=}"; shift ;;
      --db-name)
        RESTORE_DB_NAME="${2:?--db-name needs a value}"; shift 2 ;;
      --db-name=*) RESTORE_DB_NAME="${1#*=}"; shift ;;
      --force-production-target) RESTORE_FORCE_PRODUCTION_TARGET=1; shift ;;
      --force-db) RESTORE_FORCE_DB=1; shift ;;
      --force-env) RESTORE_FORCE_ENV=1; shift ;;
      --) shift; while [ $# -gt 0 ]; do RESTORE_REMAINING_ARGS+=("$1"); shift; done ;;
      *) RESTORE_REMAINING_ARGS+=("$1"); shift ;;
    esac
  done

  # Resolve the effective target root now, once, so every stage sees the
  # same value regardless of flag order.
  if [ -n "$RESTORE_STAGING_ROOT" ]; then
    RESTORE_TARGET_ROOT="${RESTORE_TARGET_ROOT:-$RESTORE_STAGING_ROOT/opt/isadoraair}"
    RESTORE_DB_NAME="${RESTORE_DB_NAME:-isadoraair_restore_test}"
  else
    RESTORE_TARGET_ROOT="${RESTORE_TARGET_ROOT:-/opt/isadoraair}"
    RESTORE_DB_NAME="${RESTORE_DB_NAME:-isadoraair}"
  fi

  log_info "mode=${RESTORE_MODE} target_root=${RESTORE_TARGET_ROOT} db_name=${RESTORE_DB_NAME}${RESTORE_STAGING_ROOT:+ staging_root=$RESTORE_STAGING_ROOT}"
}

# ---------------------------------------------------------------------
# Production-target guard (safety boundary section 6).
#
# "Looks like active IsadoraAir production" is decided by TWO independent
# signals, either of which is sufficient -- a fresh/staging box should
# trip neither:
#   1. The resolved target root IS the canonical /opt/isadoraair path
#      (i.e. no --staging-root was given), AND
#   2. At least one of the core IsadoraAir systemd units is currently
#      loaded on this host (isadoraair-gunicorn.service or
#      isadoraair-engine.service) -- checked via `systemctl show
#      -p LoadState`, which works even for a unit that's loaded but not
#      currently active/running, and does not require root.
#
# Both signals have to point the same direction on purpose: a bare-metal
# box mid-restore legitimately has /opt/isadoraair populated (that's the
# whole point of this tooling) without any unit loaded yet, and a
# --staging-root run never touches /opt/isadoraair at all regardless of
# what's loaded on the host running it -- neither of those should trip
# the guard. What SHOULD trip it: running this box's own restore tooling
# with --apply and no --staging-root while its live services are loaded,
# which is exactly the "accidentally overwrite the machine I'm
# developing on" scenario section 6 exists to prevent.
guard_production_target() {
  if [ "$RESTORE_MODE" != "apply" ]; then
    return 0  # --plan never writes, nothing to guard
  fi
  if [ -n "$RESTORE_STAGING_ROOT" ]; then
    return 0  # a staging root is never the production target root
  fi
  if [ "$RESTORE_TARGET_ROOT" != "/opt/isadoraair" ]; then
    return 0  # explicit --target-root pointed somewhere else on purpose
  fi
  local looks_live=0
  for unit in isadoraair-gunicorn.service isadoraair-engine.service; do
    if systemctl show -p LoadState --value "$unit" 2>/dev/null | grep -q '^loaded$'; then
      looks_live=1
      break
    fi
  done
  if [ "$looks_live" -eq 1 ] && [ "$RESTORE_FORCE_PRODUCTION_TARGET" -ne 1 ]; then
    log_error "Refusing: --apply with target-root=/opt/isadoraair on a host where IsadoraAir's own systemd units are loaded -- this looks like live production."
    log_error "If you really mean to restore onto THIS box's canonical path (Phase 5's actual bare-machine drill runs on a host where this is expected), re-run with --force-production-target."
    exit 1
  fi
}

# Call before any write to $RESTORE_TARGET_ROOT/.env specifically --
# guard_production_target alone is necessary but not sufficient, since a
# staging run can still target a staging .env that happens to already
# have real content from a previous partial run.
guard_env_overwrite() {
  local env_path="$1"
  if [ -e "$env_path" ] && [ -s "$env_path" ] && [ "$RESTORE_FORCE_ENV" -ne 1 ]; then
    log_error "Refusing to overwrite existing non-empty $env_path without --force-env."
    exit 1
  fi
}

# Call before pg_restore. Refuses a non-empty target database unless
# --force-db. "Non-empty" is measured by table count in the public
# schema, not just "database exists" -- an empty CREATE DATABASE shell
# (exactly what 30-postgresql.sh's own bootstrap step produces) must NOT
# require --force-db, only a database that already has real content in
# it should.
guard_db_overwrite() {
  local db_name="$1" db_user="$2" db_host="${3:-localhost}" db_port="${4:-5432}"
  local table_count
  table_count=$(PGPASSWORD="${PGPASSWORD:-}" psql -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'" 2>/dev/null || echo "")
  if [ -z "$table_count" ]; then
    return 0  # couldn't connect / database doesn't exist yet -- nothing to guard
  fi
  if [ "$table_count" -gt 0 ] && [ "$RESTORE_FORCE_DB" -ne 1 ]; then
    log_error "Refusing: database '$db_name' already has $table_count table(s) in its public schema. Restoring over it would destroy existing data. Re-run with --force-db only if you are certain."
    exit 1
  fi
}

# Never touched by ANY stage, under ANY flag combination -- not even
# --force-*. There is deliberately no override for this one. See
# docs/DISASTER_RECOVERY.md's "Music library" section and Phase 4 safety
# boundary section 13: restoring/synthesizing the 717+ GB library is
# permanently out of scope for this tooling.
guard_never_touch_music_library() {
  local path="$1"
  case "$path" in
    */srv/isadoraair/music|*/srv/isadoraair/music/*)
      log_error "Internal error: a restore stage attempted to write to $path (the music library path). This is a hard-coded refusal with no override flag -- see deploy/restore/README.md. This is a bug in the calling stage script, please report it."
      exit 1
      ;;
  esac
}

# do_or_plan CMD... -- runs CMD only in --apply mode; in --plan mode,
# logs what would run and returns success without executing it. Every
# stage script's actual filesystem/DB/systemctl mutations go through
# this (or an equivalent explicit if/else for cases too complex for a
# single command line), so `grep '\[PLAN\]'` on a --plan run is a
# complete, accurate preview of a real --apply run's actions.
do_or_plan() {
  if [ "$RESTORE_MODE" = "apply" ]; then
    log_apply "$*"
    "$@"
  else
    log_plan "$*"
  fi
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log_error "Required command not found: $1"
    exit 1
  fi
}

# ---------------------------------------------------------------------
# Runtime Foundation E7B (2026-08-29) -- the ONE place a restore stage
# locates a self-contained backup-v3 archive's embedded runtime-recovery/
# payload. The stdlib-only helper performs pre-extraction archive-member
# validation and atomic confined extraction; system tar never writes these
# root-trusted payload bytes.
# Stages 50 (native fdkaac) and 70 (TTS) are the only callers; neither
# guesses the archive layout independently -- see task step 15 /
# docs/DISASTER_RECOVERY_RESTORE.md's "Runtime recovery payload" section.
#
# restore_locate_recovery_payload DEST_DIR
#   DEST_DIR must not yet exist (or be empty) -- created here. On
#   return:
#     RESTORE_RECOVERY_PAYLOAD_FOUND=1  DEST_DIR IS the payload root
#                                        (runtime-recovery.json directly
#                                        inside it) -- extracted, and its
#                                        basic confinement/shape checked,
#                                        but NOT yet validated for
#                                        integrity -- that is Python's
#                                        job (validate_runtime_recovery_payload
#                                        / load_recovery_payload), never
#                                        re-implemented here.
#     RESTORE_RECOVERY_PAYLOAD_FOUND=0  legacy/v2.x or explicitly
#                                        non-self-contained archive.
#                                        Callers fail backup-based DR;
#                                        legacy recovery must be selected
#                                        explicitly and never falls back.
#
# Returns nonzero only for a genuine extraction failure (corrupt
# archive, unwritable DEST_DIR, ...).
restore_locate_recovery_payload() {
  local dest="$1"
  require_cmd python3
  if [ -z "$RESTORE_ARCHIVE" ] || [ ! -f "$RESTORE_ARCHIVE" ]; then
    log_error "restore_locate_recovery_payload: no valid --archive given."
    return 1
  fi
  local helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/runtime_recovery_archive.py"
  local status=0
  python3 "$helper" extract --archive "$RESTORE_ARCHIVE" --destination "$dest" || status=$?
  if [ "$status" -eq 2 ] || [ "$status" -eq 3 ]; then
    RESTORE_RECOVERY_PAYLOAD_FOUND=0
    return 0
  fi
  if [ "$status" -ne 0 ]; then
    log_error "restore_locate_recovery_payload: safe extraction failed."
    return 1
  fi
  RESTORE_RECOVERY_PAYLOAD_FOUND=1
  return 0
}

restore_recovery_receipt_path() {
  if [ -n "$RESTORE_STAGING_ROOT" ]; then
    printf '%s\n' "$RESTORE_STAGING_ROOT/var/lib/isadoraair/restore/runtime-recovery.json"
  else
    printf '%s\n' "/var/lib/isadoraair/restore/runtime-recovery.json"
  fi
}

restore_record_recovery_components() {
  local receipt
  receipt=$(restore_recovery_receipt_path)
  local helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/runtime_recovery_archive.py"
  local args=()
  local component
  for component in "$@"; do
    args+=(--component "$component")
  done
  python3 "$helper" record --archive "$RESTORE_ARCHIVE" --receipt "$receipt" "${args[@]}"
}

restore_accept_recovery_receipt() {
  local receipt
  receipt=$(restore_recovery_receipt_path)
  local helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/runtime_recovery_archive.py"
  python3 "$helper" accept --archive "$RESTORE_ARCHIVE" --receipt "$receipt"
}
