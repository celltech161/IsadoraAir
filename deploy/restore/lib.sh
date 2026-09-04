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

# The current restore checkout's own root -- computed once from lib.sh's
# own location, since every stage script sources lib.sh from the same
# tree. This is the recovery SOURCE authority (see restore_manage below);
# never confuse it with $RESTORE_TARGET_ROOT, the tree being restored.
RESTORE_REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

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

# Fixed marker for operator-facing descriptions of sensitive actions.
# Deliberately does not expose even the secret's length.
redact() { printf '<redacted>'; }

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

  # Runtime Foundation E7C (2026-08-29) restore-safety fix: 30-postgresql.sh
  # pg_restores into the isolated $RESTORE_DB_NAME under --staging-root, but
  # $RESTORE_TARGET_ROOT/.env is a byte-faithful, UNMODIFIED copy of the
  # real station's .env (see 20-application.sh's own header -- rewriting it
  # here would make the staged tree wrong for eventual real restoration).
  # Every later manage.py invocation (60-python.sh, 50-native-deps.sh,
  # 70-tts.sh, 95-validate.sh) reads DB_NAME via python-decouple's config(),
  # which checks the real OS environment BEFORE .env -- so exporting it
  # once, here, at the one place every stage already resolves
  # RESTORE_DB_NAME, makes every later manage.py call in this stage's own
  # process automatically and deterministically target the SAME database
  # pg_restore just used, with no stage-specific code and no operator-
  # remembered manual export. Production restores are unaffected: this
  # exports exactly "isadoraair" (or an explicit --db-name), the same value
  # .env already carries for a real restore of this station.
  export DB_NAME="$RESTORE_DB_NAME"

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

# ---------------------------------------------------------------------
# ensure_confined_directory ROOT TARGET MODE UID GID
#
# Runtime Foundation E7D (2026-09-04) -- the shared, confined directory-
# establishment primitive 90-system-config.sh uses to build the legacy
# scratch-tmpfiles surface (/run/isadoraair, /run/isadoraair/tts) inside
# an isolated --staging-root, mirroring isadoraair/runtime_native.py's
# _ensure_noncanonical_publication_directories -- the Python-side sibling
# of this exact same safety contract for E4's native-publication target
# skeleton. See that function's own docstring for the parallel reasoning.
#
# Creates TARGET (and any missing ancestor between ROOT and TARGET) as a
# real, non-symlink directory, one path component at a time:
#   - ROOT itself must already be a real, non-symlink, existing directory.
#   - TARGET must fall beneath ROOT -- anything else is refused before
#     touching the filesystem at all (this is what keeps a staging
#     establish from ever reaching the real host /run).
#   - Each EXISTING ancestor component is validated as a real,
#     non-symlink directory and left otherwise untouched (never
#     chmodded/chowned merely because it already existed).
#   - Each MISSING ancestor component is created fresh at a fixed, safe
#     0755 mode.
#   - TARGET itself (the final component) always gets MODE/UID:GID
#     explicitly (re-)asserted, whether newly created or pre-existing --
#     this mirrors what a tmpfiles.d `d` line itself does at every boot,
#     since this establishes the exact directories deploy/isadoraair-
#     tmpfiles.conf already declares, never a second competing authority
#     for them.
# Never follows or replaces a symlink; never deletes anything. Fails
# closed (logs a clear diagnostic and exits 1) on any symlink ancestor,
# non-directory collision, an escape outside ROOT, or an ownership this
# caller cannot actually establish (e.g. an unprivileged staging run
# requesting a UID/GID other than its own) -- NEVER silently substitutes
# a different identity than the one requested.
ensure_confined_directory() {
  local root="$1" target="$2" mode="$3" uid="$4" gid="$5"
  if [ -L "$root" ] || [ ! -d "$root" ]; then
    log_error "ensure_confined_directory: root is not a real, existing directory: $root"
    exit 1
  fi
  case "$target" in
    "$root"|"$root"/*) ;;
    *) log_error "ensure_confined_directory: $target escapes root $root"; exit 1 ;;
  esac
  local relative="${target#"$root"}"
  relative="${relative#/}"
  local cursor="$root"
  local parts=()
  if [ -n "$relative" ]; then
    IFS='/' read -ra parts <<< "$relative"
  fi
  local part
  for part in "${parts[@]}"; do
    [ -z "$part" ] && continue
    cursor="$cursor/$part"
    if [ -L "$cursor" ]; then
      log_error "ensure_confined_directory: unexpected symlink at $cursor"
      exit 1
    fi
    if [ -e "$cursor" ]; then
      if [ ! -d "$cursor" ]; then
        log_error "ensure_confined_directory: $cursor exists and is not a directory"
        exit 1
      fi
    else
      if ! mkdir -m 0755 "$cursor" 2>/dev/null || [ -L "$cursor" ] || [ ! -d "$cursor" ]; then
        log_error "ensure_confined_directory: failed to create a real directory at $cursor"
        exit 1
      fi
    fi
  done
  if [ -L "$target" ] || [ ! -d "$target" ]; then
    log_error "ensure_confined_directory: $target is not a plain directory"
    exit 1
  fi
  chmod "$mode" "$target"
  if ! chown "$uid:$gid" "$target" 2>/dev/null; then
    log_error "ensure_confined_directory: cannot set ownership $uid:$gid on $target -- this caller does not have permission to establish that identity here (an unprivileged staging run can normally only chown to its own uid/gid). Supply --isa-uid/--isa-gid matching an identity this run can actually establish, or run with sufficient privilege. Never silently substituting a different identity."
    exit 1
  fi
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

# do_or_plan_redacted SAFE_DESCRIPTION CMD... -- equivalent to
# do_or_plan, but never derives its log line from CMD or its arguments.
# Use this whenever CMD receives a secret through argv, stdin, or its
# environment. SAFE_DESCRIPTION must contain only non-secret context and
# an explicit <redacted> marker where useful to the operator.
do_or_plan_redacted() {
  local safe_description="${1:?safe description required}"
  shift
  if [ "$RESTORE_MODE" = "apply" ]; then
    log_apply "$safe_description"
    "$@"
  else
    log_plan "$safe_description"
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
# Stages 50 (native fdkaac), 70 (TTS), and 75 (protected updater) are
# the only callers; none guesses the archive layout independently -- see task step 15 /
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

# ---------------------------------------------------------------------
# Restore management authority (Runtime Foundation E7C, 2026-09-04).
#
# Stages 50 (native fdkaac), 70 (TTS), 75 (protected updater), and 90 (E5
# system surfaces) all REPAIR or PROVISION runtime state by invoking a
# manage.py management command against the restored target. Naively
# running "$RESTORE_TARGET_ROOT/venv/bin/python" "$RESTORE_TARGET_ROOT/
# manage.py" -- as every one of them once did -- makes the RESTORED
# BACKUP's OWN recovery code authoritative for its own repair: a newer
# restore checkout can no longer fix a defect in an older, otherwise-
# compatible backup's runtime-recovery implementation, because the fix
# never runs -- the backup's stale copy of the same command does. This
# is not acceptable for backward-compatible disaster recovery.
#
# restore_manage CMD [ARGS...] is the one shared call every stage that
# needs to run a manage.py command against a restored target should use
# instead of inventing its own venv/manage.py invocation. It runs
# restore_manage.py (a stdlib-only helper, see that file's own docstring
# for the full contract), which:
#   - always executes THIS checkout's manage.py ($RESTORE_REPO_ROOT) --
#     never $RESTORE_TARGET_ROOT/manage.py, no matter what;
#   - under the RESTORED target's own venv Python interpreter (it already
#     has Django + every runtime dependency 60-python.sh installed), but
#     ONLY after verifying that interpreter's installed packages exactly
#     satisfy THIS checkout's own requirements.txt pins -- on any
#     mismatch this fails closed (nonzero exit, logged below) and NEVER
#     falls back to the restored target's own manage.py;
#   - relays $RESTORE_TARGET_ROOT/.env into the real OS environment
#     first, so the restored station's own configuration/secrets remain
#     authoritative for the command about to run (python-decouple's
#     config() always checks os.environ before any file -- see
#     restore_manage.py's own docstring) without ever copying .env into
#     this checkout and without decouple's own file-search risking a
#     stray developer/sandbox .env instead. Anything the caller's own
#     shell already exported (e.g. this file's own DB_NAME staging
#     override above) is left untouched -- it already wins by the exact
#     same os.environ-first rule.
#
# $RESTORE_TARGET_ROOT/venv and $RESTORE_TARGET_ROOT/.env must already
# exist (60-python.sh, 20-application.sh) -- checked here with the same
# clear diagnostics every stage already gave inline, so no caller-visible
# behavior changes for that failure mode.
#
# restore_manage_command populates the global array RESTORE_MANAGE_CMD
# with the fully-resolved argv (never executes it) -- use this directly,
# instead of restore_manage below, when the caller needs to run the
# result under sudo (bash functions aren't visible to a separate sudo
# process; a real argv is) -- see 75-protected-updater.sh's real
# (non-staging) publish path for the one caller that needs this.
restore_manage_command() {
  local venv_python="$RESTORE_TARGET_ROOT/venv/bin/python"
  if [ ! -x "$venv_python" ]; then
    log_error "restore_manage: $venv_python not found -- run 60-python.sh first (it builds the restored target's Python environment that this checkout's recovery authority runs under)."
    exit 1
  fi
  if [ ! -f "$RESTORE_TARGET_ROOT/.env" ]; then
    log_error "restore_manage: $RESTORE_TARGET_ROOT/.env not found -- run 20-application.sh first."
    exit 1
  fi
  local helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/restore_manage.py"
  RESTORE_MANAGE_CMD=(
    "$venv_python" "$helper"
    --repo-root "$RESTORE_REPO_ROOT"
    --target-root "$RESTORE_TARGET_ROOT"
    -- "$@"
  )
}

# restore_manage CMD [ARGS...] -- resolves + immediately runs. The
# ordinary case every caller except 75-protected-updater.sh's real-root
# publish step (sudo) wants.
restore_manage() {
  local RESTORE_MANAGE_CMD=()
  restore_manage_command "$@"
  "${RESTORE_MANAGE_CMD[@]}"
}
