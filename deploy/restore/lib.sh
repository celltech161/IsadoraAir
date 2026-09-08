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
RESTORE_RESUME=0
RESTORE_ADOPT_PRE_LEDGER=0
RESTORE_MEDIA_ROOT=""
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
      --resume) RESTORE_RESUME=1; shift ;;
      --adopt-pre-ledger) RESTORE_ADOPT_PRE_LEDGER=1; shift ;;
      --recovery-media-root)
        RESTORE_MEDIA_ROOT="${2:?--recovery-media-root needs a path}"; shift 2 ;;
      --recovery-media-root=*) RESTORE_MEDIA_ROOT="${1#*=}"; shift ;;
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

  local resume_suffix=""
  [ "$RESTORE_RESUME" -eq 1 ] && resume_suffix=" resume=1"
  [ "$RESTORE_ADOPT_PRE_LEDGER" -eq 1 ] && resume_suffix="${resume_suffix} adopt_pre_ledger=1"
  log_info "mode=${RESTORE_MODE} target_root=${RESTORE_TARGET_ROOT} db_name=${RESTORE_DB_NAME}${RESTORE_STAGING_ROOT:+ staging_root=$RESTORE_STAGING_ROOT}${resume_suffix}"
}

# ---------------------------------------------------------------------
# restore_default_companions_root -- the SAME default 80-companions.sh
# itself resolves COMPANIONS_ROOT from (${RESTORE_STAGING_ROOT:-$HOME}),
# pulled out here as the single shared source of truth. r0043: this is
# also exactly the root the "known legacy WEATHER_DATA_DIR" recognition
# check (40-station-content.sh) and the provenance-checked scaffold
# repair (80-companions.sh) both need -- a companion project's own
# SOURCE CHECKOUT namespace is always "<this root>/<repo-name>",
# regardless of an operator's later --companions-root override at
# Stage 80 (a legacy .env value predates, and is independent of, any
# such override choice made now).
# ---------------------------------------------------------------------
restore_default_companions_root() {
  printf '%s\n' "${RESTORE_STAGING_ROOT:-$HOME}"
}

# _restore_is_known_empty_scaffold DIR -- true (exit 0) only if DIR
# exists, is a real (non-symlink) directory, contains no .git anywhere,
# and its ENTIRE recursive content is real directories only -- zero
# regular files, zero symlinks anywhere in the tree. r0043: this is the
# exact, narrow signature the legacy-WEATHER_DATA_DIR defect leaves
# behind -- weather/services.py's own module-level `DATA_DIR.mkdir(
# parents=True, exist_ok=True)` creates ONLY the empty directory tree,
# never a single file; an application that had actually received real
# weather data, or any other real content, would have written at least
# one regular file somewhere in that tree. Used by 80-companions.sh's
# own narrowly-scoped, ledger-provenance-gated repair -- never a
# general "trust any empty directory" primitive on its own.
_restore_is_known_empty_scaffold() {
  local dir="$1"
  if [ -L "$dir" ] || [ ! -d "$dir" ]; then
    return 1
  fi
  if [ -e "$dir/.git" ]; then
    return 1
  fi
  if find "$dir" \( -type f -o -type l \) -print -quit 2>/dev/null | grep -q .; then
    return 1
  fi
  return 0
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
  # RESTORE_RECOVERY_RECEIPT_ROOT is a test/override seam only -- unset
  # in every real invocation, so real behavior (real canonical path, or
  # the real --staging-root-relative path) is completely unchanged. It
  # exists because a canonical-mode functional test needs the NATIVE_
  # TARGET_ROOT/TTS_TARGET_ROOT half of a stage's behavior to stay real
  # ("/"), while never touching the real host's
  # /var/lib/isadoraair/restore -- two independent concerns
  # --staging-root alone cannot separate, since it redirects both.
  if [ -n "${RESTORE_RECOVERY_RECEIPT_ROOT:-}" ]; then
    printf '%s\n' "$RESTORE_RECOVERY_RECEIPT_ROOT/var/lib/isadoraair/restore/runtime-recovery.json"
  elif [ -n "$RESTORE_STAGING_ROOT" ]; then
    printf '%s\n' "$RESTORE_STAGING_ROOT/var/lib/isadoraair/restore/runtime-recovery.json"
  else
    printf '%s\n' "/var/lib/isadoraair/restore/runtime-recovery.json"
  fi
}

# r0041: on a genuinely fresh machine, no earlier restore stage creates
# /var/lib/isadoraair/restore -- an ordinary operator account cannot
# create it beneath root-owned /var/lib (empirically confirmed: a bare
# `mkdir /var/lib/isadoraair/restore` as the restore's own unprivileged
# account fails with Permission denied). Stages 50/70/75 all record
# into this same receipt after a real canonical publish already
# escalated under sudo for their own main operation -- this establishes
# ONLY the receipt directory's existence/ownership, narrowly and
# idempotently, using the exact same sudo-mkdir-then-chown-the-leaf
# idiom 40-station-content.sh's own ensure_dir() already uses for
# /var/lib/isadoraair/reports (never broadens /var/lib/isadoraair
# itself -- a mix of root-owned siblings, e.g. .../tts, and operator-
# owned ones, e.g. .../reports and this directory, is the existing,
# intentional design; every directory below /var/lib/isadoraair only
# needs the PARENT to remain traversable, 0755, never writable, which a
# bare `mkdir -p` already leaves it as). The receipt's own content --
# atomic write, schema validation, archive/payload identity checks,
# fail-closed behavior -- stays entirely inside runtime_recovery_archive.py's
# existing record command, run completely UNPRIVILEGED once this
# directory is owned by the caller: privilege here is scoped to
# directory establishment only, never receipt content.
_restore_ensure_recovery_receipt_dir() {
  local receipt_dir parent
  receipt_dir="$(dirname "$(restore_recovery_receipt_path)")"
  parent="$(dirname "$receipt_dir")"
  # Checked unconditionally, in every mode -- not just the real
  # canonical path -- so a staging/override tree gets the exact same
  # confinement guarantee, never a second, weaker convention.
  if [ -L "$receipt_dir" ]; then
    log_error "refusing: $receipt_dir is a symlink, not a real directory -- will not create or chown through it."
    exit 1
  fi
  if [ -L "$parent" ]; then
    log_error "refusing: $parent is a symlink, not a real directory -- will not create or chown through it."
    exit 1
  fi
  if [ -n "$RESTORE_STAGING_ROOT" ] || [ -n "${RESTORE_RECOVERY_RECEIPT_ROOT:-}" ]; then
    mkdir -p "$receipt_dir"
    chmod 0755 "$receipt_dir"
    return 0
  fi
  sudo mkdir -p "$receipt_dir"
  if [ -L "$receipt_dir" ]; then
    log_error "refusing: $receipt_dir became a symlink during establishment -- aborting."
    exit 1
  fi
  # Deterministic regardless of root's own umask -- the same class of
  # bug as the backup producer's runtime-recovery/ root (see
  # backup_isadoraair.sh's own comment): `mkdir -p` alone leaves this at
  # whatever mode 0777-minus-umask happens to produce, not a fixed 0755.
  sudo chmod 0755 "$receipt_dir"
  sudo chown "$(id -u):$(id -g)" "$receipt_dir"
}

# ---------------------------------------------------------------------
# Restore-session ledger (Runtime Foundation, r0043) -- see
# restore_ledger.py's own module docstring for the full identity/
# fail-closed contract. Lives in the SAME /var/lib/isadoraair/restore/
# directory as the recovery receipt above, established the same
# narrow, sudo-then-chown-to-caller way -- never a second, broader
# writable surface.
#
# restore_ledger_path -- same staging/override-root resolution as
# restore_recovery_receipt_path (including the SAME
# RESTORE_RECOVERY_RECEIPT_ROOT test/override seam -- there is
# deliberately no second override variable for the ledger; it is the
# same directory, so the same seam already relocates it correctly).
restore_ledger_path() {
  if [ -n "${RESTORE_RECOVERY_RECEIPT_ROOT:-}" ]; then
    printf '%s\n' "$RESTORE_RECOVERY_RECEIPT_ROOT/var/lib/isadoraair/restore/ledger.json"
  elif [ -n "$RESTORE_STAGING_ROOT" ]; then
    printf '%s\n' "$RESTORE_STAGING_ROOT/var/lib/isadoraair/restore/ledger.json"
  else
    printf '%s\n' "/var/lib/isadoraair/restore/ledger.json"
  fi
}

# restore_ledger_record STAGE [--git-sha SHA] [--payload-id ID]
#   [--product-contract-sha256 SHA] [--detail JSON]
#
# Records STAGE as durably complete for the CURRENT --archive/
# --target-root. A no-op (returns 0, writes nothing) when the calling
# stage doesn't take --archive at all (60-python.sh, 90-system-
# config.sh, 95-validate.sh currently don't) -- those stages' own
# completion is not meaningfully bindable to an archive identity they
# never receive, and every OTHER stage that DOES take --archive already
# establishes/verifies that same identity before they would ever run.
#
# Called as a bare command (never inside a `$(...)` substitution) so
# `set -e` aborts the calling stage script immediately, with
# restore_ledger.py's own clear stderr message, on any identity
# mismatch or ledger corruption -- exactly the "wrong restore-session/
# archive identity" and "corrupted/incomplete ledger" hard-failure
# cases the r0043 safety boundary requires.
restore_ledger_record() {
  local stage="$1"; shift
  if [ -z "$RESTORE_ARCHIVE" ] || [ ! -f "$RESTORE_ARCHIVE" ]; then
    return 0
  fi
  _restore_ensure_recovery_receipt_dir
  local ledger; ledger="$(restore_ledger_path)"
  local helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/restore_ledger.py"
  python3 "$helper" record --ledger "$ledger" --archive "$RESTORE_ARCHIVE" --target-root "$RESTORE_TARGET_ROOT" --stage "$stage" "$@"
}

# restore_ledger_stage_state STAGE -- prints "complete" or "absent" to
# stdout, exit 0. Exits nonzero (restore_ledger.py's own clear stderr
# message) if an existing ledger belongs to a different archive/target
# -- callers MUST NOT swallow that exit status (assign via a bare
# `VAR=$(...) || exit 1` / explicit `if` check, never `local
# VAR=$(...)`, whose own exit status is the `local` builtin's, not the
# substituted command's -- see each caller for the exact idiom used).
restore_ledger_stage_state() {
  local stage="$1"
  if [ -z "$RESTORE_ARCHIVE" ] || [ ! -f "$RESTORE_ARCHIVE" ]; then
    printf 'absent\n'
    return 0
  fi
  local ledger; ledger="$(restore_ledger_path)"
  local helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/restore_ledger.py"
  python3 "$helper" stage-state --ledger "$ledger" --archive "$RESTORE_ARCHIVE" --target-root "$RESTORE_TARGET_ROOT" --stage "$stage"
}

restore_record_recovery_components() {
  local receipt
  receipt=$(restore_recovery_receipt_path)
  _restore_ensure_recovery_receipt_dir
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

# restore_archive_recovery_metadata -- prints $RESTORE_ARCHIVE's own
# embedded runtime-recovery-archive.json metadata (payload_id,
# included_components, recovery_class, ...) as JSON to stdout, or
# nothing + nonzero exit for a legacy/non-self-contained archive (or if
# $RESTORE_ARCHIVE isn't set at all). Read-only, never extracts the
# payload itself -- cheap enough to call speculatively (r0044's TTS
# adoption check uses this to learn which components an archive
# declares BEFORE deciding whether extracting/validating the full
# payload is even worth doing).
restore_archive_recovery_metadata() {
  if [ -z "$RESTORE_ARCHIVE" ] || [ ! -f "$RESTORE_ARCHIVE" ]; then
    return 1
  fi
  local helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/runtime_recovery_archive.py"
  python3 "$helper" inspect --archive "$RESTORE_ARCHIVE" 2>/dev/null
}

# ---------------------------------------------------------------------
# Recovery media (r0045) -- see recovery_media.py's own module
# docstring for the full layout contract this discovers/validates
# against (established from build_offline_closure.py's own --out-dir
# structure, never guessed). Read-only; never mutates anything.
#
# restore_media_validate ROOT [ARCHIVE] -- prints the JSON validation
# evidence to stdout, returns 0 only if ROOT is a structurally complete
# recovery-media tree (and, when ARCHIVE is given, ROOT's backups/
# actually contains that exact archive by SHA256).
restore_media_validate() {
  local root="$1" archive="${2:-}"
  local helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/recovery_media.py"
  if [ -n "$archive" ]; then
    python3 "$helper" validate --root "$root" --archive "$archive"
  else
    python3 "$helper" validate --root "$root"
  fi
}

# restore_media_discover ARCHIVE [SEARCH_ROOT...] -- prints a JSON array
# of every structurally-valid recovery-media root found (archive-
# matching roots sorted first), to stdout. Empty array (`[]`, exit 1)
# means nothing valid was found at all.
restore_media_discover() {
  local archive="$1"; shift
  local helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/recovery_media.py"
  local args=(discover --archive "$archive")
  local search_root
  for search_root in "$@"; do
    args+=(--search-root "$search_root")
  done
  python3 "$helper" "${args[@]}"
}

# restore_media_detect_apt_groups ROOT -- prints
# {"OPTIONAL_CD_RIP":bool,...} JSON for which optional
# deploy/packages-ubuntu-26.04.txt groups ROOT's own frozen apt closure
# actually includes -- data-driven from ROOT's own manifest, never
# hard-coded.
restore_media_detect_apt_groups() {
  local root="$1"
  local helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/recovery_media.py"
  local packages_file="$RESTORE_REPO_ROOT/deploy/packages-ubuntu-26.04.txt"
  python3 "$helper" detect-apt-groups --root "$root" --packages-file "$packages_file"
}

# restore_verify_component_receipt COMPONENT -- r0044 pre-ledger adoption
# support for Stages 50/70/75: proves, WITHOUT republishing anything,
# that COMPONENT was already durably recovered from THIS EXACT archive
# using evidence that predates the ledger entirely (the runtime-
# recovery receipt record_components already writes on a real publish,
# plus this archive's own embedded metadata). Exit code 0 = verified;
# nonzero (clear stderr message from runtime_recovery_archive.py) =
# not provably recovered from this exact archive -- callers must treat
# any nonzero exit as "adoption not proven," never a fallback pass.
restore_verify_component_receipt() {
  local component="$1"
  local receipt
  receipt=$(restore_recovery_receipt_path)
  local helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/runtime_recovery_archive.py"
  python3 "$helper" verify-component-receipt --archive "$RESTORE_ARCHIVE" --receipt "$receipt" --component "$component"
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
# process; a real argv is) -- see 75-protected-updater.sh's and (r0041)
# 50-native-deps.sh's and 70-tts.sh's own real (non-staging) canonical
# publish steps, the three callers that need this. All three share the
# same USE_SUDO idiom: unprivileged for everything else (50-native-
# deps.sh's own prepare phase deliberately stays on the plain,
# never-sudo restore_manage wrapper below -- see that script's own
# comment on the E4 prepare/publish trust handoff) except that one
# final privileged invocation.
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
# ordinary case every caller except 75-protected-updater.sh's,
# 50-native-deps.sh's, and 70-tts.sh's own real-root publish steps
# (sudo) wants.
restore_manage() {
  local RESTORE_MANAGE_CMD=()
  restore_manage_command "$@"
  "${RESTORE_MANAGE_CMD[@]}"
}
