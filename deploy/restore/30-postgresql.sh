#!/usr/bin/env bash
# deploy/restore/30-postgresql.sh -- IsadoraAir 1.2 Phase 4.
#
# PostgreSQL bootstrap (role + database, matching docs/DISASTER_RECOVERY.md's
# documented sequence, made explicit/deterministic about encoding+locale
# rather than relying on cluster defaults -- see below) + pg_restore of
# the backup's database.dump.
#
# Reads DB_USER/DB_PASSWORD/DB_HOST/DB_PORT from the ALREADY-RESTORED
# .env at the target root (20-application.sh must run first) -- never
# invents a password. The database NAME actually operated on is
# $RESTORE_DB_NAME (isadoraair in a real restore, isadoraair_restore_test
# under --staging-root), which can differ from whatever DB_NAME literally
# says inside .env -- that's intentional, it's what lets a staging run
# restore into an isolated database using the exact same real credentials
# without editing .env.
#
# Guarded by guard_db_overwrite: refuses to pg_restore over a database
# that already has tables, unless --force-db.
#
# Usage:
#   deploy/restore/30-postgresql.sh --archive PATH [--plan|--apply]
#     [--staging-root PATH] [--force-db]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

restore_parse_common_args "$@"

log_info "=== 30-postgresql ==="
guard_production_target
require_cmd psql
require_cmd pg_restore
require_cmd createuser
require_cmd tar

if [ -z "$RESTORE_ARCHIVE" ] || [ ! -f "$RESTORE_ARCHIVE" ]; then
  log_error "No valid --archive given. Run 00-preflight.sh first."
  exit 1
fi

ENV_FILE="$RESTORE_TARGET_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
  log_error ".env not found at $ENV_FILE -- run 20-application.sh first (it restores .env, which this stage reads DB credentials from)."
  exit 1
fi

DB_USER=$(grep -E '^DB_USER=' "$ENV_FILE" | head -1 | cut -d= -f2-)
DB_PASSWORD=$(grep -E '^DB_PASSWORD=' "$ENV_FILE" | head -1 | cut -d= -f2-)
DB_HOST=$(grep -E '^DB_HOST=' "$ENV_FILE" | head -1 | cut -d= -f2-)
DB_PORT=$(grep -E '^DB_PORT=' "$ENV_FILE" | head -1 | cut -d= -f2-)
DB_HOST="${DB_HOST:-localhost}"
DB_PORT="${DB_PORT:-5432}"
: "${DB_USER:?DB_USER not found in $ENV_FILE}"
: "${DB_PASSWORD:?DB_PASSWORD not found in $ENV_FILE}"
export PGPASSWORD="$DB_PASSWORD"
log_info "DB target: ${DB_USER}@${DB_HOST}:${DB_PORT}/${RESTORE_DB_NAME} (password read from .env, not logged)"

# ---- Resume: verify already-durably-completed work rather than
#      re-running role bootstrap/pg_restore -- r0043. Same ledger-
#      identity + independent-verification contract as
#      20-application.sh's own resume branch: a "complete" ledger state
#      is NEVER trusted alone -- the actual database is independently
#      re-queried for the exact same evidence this stage's own post-
#      restore verification (section 6 below) already establishes. A
#      ledger that does not yet record this stage complete falls
#      through to today's exact behavior unchanged (a non-empty
#      database still requires --force-db without a matching completed
#      ledger entry -- unknown/non-restore-owned content stays
#      fail-closed).
if [ "$RESTORE_RESUME" -eq 1 ] && [ "$RESTORE_MODE" = "apply" ]; then
  STAGE_STATE=$(restore_ledger_stage_state "30-postgresql") || exit 1
  if [ "$STAGE_STATE" = "complete" ]; then
    log_info "30-postgresql: --resume -- ledger records this stage already complete for this exact archive/target; verifying durable output rather than re-running pg_restore."
    RESUME_TABLE_COUNT=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$RESTORE_DB_NAME" -tAc \
      "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'" 2>/dev/null || echo "")
    RESUME_MIGRATIONS_EXISTS=""
    if [ -n "$RESUME_TABLE_COUNT" ] && [ "$RESUME_TABLE_COUNT" -gt 0 ] 2>/dev/null; then
      RESUME_MIGRATIONS_EXISTS=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$RESTORE_DB_NAME" -tAc \
        "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'django_migrations'" 2>/dev/null || echo "")
    fi
    if [ -z "$RESUME_TABLE_COUNT" ] || [ "$RESUME_TABLE_COUNT" -eq 0 ] || [ "$RESUME_MIGRATIONS_EXISTS" != "1" ]; then
      log_error "30-postgresql: resume verification FAILED -- the ledger records this stage already complete, but database '$RESTORE_DB_NAME' does not currently hold the expected restored content (table_count=${RESUME_TABLE_COUNT:-<unreachable>}, django_migrations present=${RESUME_MIGRATIONS_EXISTS:-no}). Ledger and database disagree -- refusing to guess which is authoritative; investigate manually (or remove the stale ledger entry at $(restore_ledger_path)) before retrying."
      exit 1
    fi
    log_info "30-postgresql: resume verification PASS -- $RESUME_TABLE_COUNT table(s), django_migrations present. Not re-running pg_restore."
    restore_ledger_record "30-postgresql"
    log_info "30-postgresql: PASS (resumed/verified)"
    exit 0
  fi
  log_info "30-postgresql: --resume given, but the ledger does not yet record this stage complete for this archive/target -- proceeding with the normal restore below."
fi

# r0036 fed createuser --pwprompt's two "Enter password" prompts over a
# stdin pipe, on the assumption that createuser always reads them from
# there. Real PostgreSQL does not: its password prompt (simple_prompt())
# opens /dev/tty directly whenever a controlling terminal is available,
# and reads/echoes-off there instead -- completely bypassing stdin. This
# was confirmed empirically against the installed createuser/psql 18.6
# binaries (Ubuntu 26.04.1, matching E8's target OS): with no controlling
# terminal the piped password IS consumed correctly (this is why r0036's
# own mocked-createuser test and even a quick manual check both looked
# fine), but under a real controlling tty -- exactly what an operator's
# interactive session or a pty-allocating orchestrator gives a restore
# run -- createuser instead blocks on /dev/tty, or (per E8) reads
# whatever unrelated input arrives there, silently creating the role
# with the WRONG password while still exiting 0. This is the r0040
# regression fix; --pwprompt must never be used here again.
#
# Replacement: create the role with NO password at all (createuser
# --no-password has nothing to prompt for), then set the password
# separately via SQL. DB_PASSWORD never appears in this script's own SQL
# text, in any command's argv, or in anything do_or_plan/log_apply would
# echo:
#   1. It is written to a private (mode 0600, owner-only) temp file as a
#      plain shell assignment, using bash's own `printf '%q'` quoting --
#      not manual/ad-hoc escaping -- so every byte (quotes, $, backslashes,
#      whitespace) round-trips exactly when the file is later `source`d.
#   2. Ownership passes to the `postgres` OS user -- the one privilege
#      crossing already required for every other `sudo -u postgres` call
#      in this stage -- so only postgres/root can read it. This
#      deliberately avoids `sudo --preserve-env`: whether that is even
#      permitted depends on the target's own sudoers policy, which
#      cannot be verified ahead of time against a not-yet-provisioned E8
#      box, whereas a private file postgres already owns needs no such
#      grant.
#   3. The postgres-owned process sources that file, then feeds a STATIC
#      SQL script (no secret, no shell interpolation -- see the heredoc
#      below) to psql on its own stdin. That script uses psql's
#      `\getenv` to read the password straight out of its own process
#      environment, and `:'var'` literal-substitution to have psql --
#      never this shell -- do the SQL string-quoting.
set_postgresql_role_password() {
  local envfile sqlfile status=0
  envfile="$(mktemp /tmp/isadoraair-restore-role-env.XXXXXX)"
  sqlfile="$(mktemp /tmp/isadoraair-restore-role.XXXXXX.sql)"
  chmod 600 "$envfile"
  printf 'RESTORE_ROLE_NAME=%q\nRESTORE_ROLE_PASSWORD=%q\n' "$DB_USER" "$DB_PASSWORD" > "$envfile"
  cat > "$sqlfile" <<'SQL'
\getenv restore_role_name RESTORE_ROLE_NAME
\getenv restore_role_password RESTORE_ROLE_PASSWORD
\if :{?restore_role_name}
\else
\warn 'RESTORE_ROLE_NAME missing from environment -- refusing to guess'
\quit 1
\endif
\if :{?restore_role_password}
\else
\warn 'RESTORE_ROLE_PASSWORD missing from environment -- refusing to guess'
\quit 1
\endif
ALTER ROLE :"restore_role_name" PASSWORD :'restore_role_password';
SQL
  # r0042 sibling audit (found while tracing 75-protected-updater.sh's
  # own privileged-scratch-in-caller-tree defect): unlike that case,
  # these two files are still jreed-owned right up until the chown
  # below, so an ordinary rm can always reach them -- but under `set -e`
  # a failing chown here would abort this whole script before EITHER
  # cleanup line below ever ran, leaving a stray mode-600 file holding
  # DB_PASSWORD in shell-quoted form. Guarded explicitly rather than
  # relying on set -e to do the right thing.
  if ! sudo chown postgres:postgres "$envfile" "$sqlfile"; then
    rm -f "$envfile" "$sqlfile"
    return 1
  fi
  sudo -u postgres bash -c 'set -a; source "$1"; set +a; exec psql -v ON_ERROR_STOP=1 -d postgres -f "$2"' _ "$envfile" "$sqlfile" || status=$?
  sudo rm -f "$envfile" "$sqlfile"
  return "$status"
}

create_postgresql_role_with_password() {
  sudo -u postgres createuser --no-password "$DB_USER"
  set_postgresql_role_password
}

# ---- 1. Role + database existence (read-only; safe under --plan too, and
#         needed together to decide the role-bootstrap path below) --------
ROLE_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname = '${DB_USER}'" 2>/dev/null || true)
ROLE_IS_SUPERUSER=""
if [ "$ROLE_EXISTS" = "1" ]; then
  ROLE_IS_SUPERUSER=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname = '${DB_USER}' AND rolsuper" 2>/dev/null || true)
fi
DB_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${RESTORE_DB_NAME}'" 2>/dev/null || true)
TARGET_DB_TABLE_COUNT=""
if [ "$DB_EXISTS" = "1" ]; then
  TARGET_DB_TABLE_COUNT=$(sudo -u postgres psql -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'" \
    -d "$RESTORE_DB_NAME" 2>/dev/null || true)
fi
TARGET_DB_HAS_CONTENT=0
if [ "$DB_EXISTS" = "1" ] && [ "${TARGET_DB_TABLE_COUNT:-0}" -gt 0 ] 2>/dev/null; then
  TARGET_DB_HAS_CONTENT=1
fi

# ---- 2. Role bootstrap ----------------------------------------------------
# A failed E8 run can leave the role present with the WRONG password
# (exactly the r0036 bug above) while the target database is still empty
# or absent -- an immediate rerun must converge on the correct password
# instead of forever replaying it, without ever touching a role/database
# this tool doesn't unambiguously own. State handled:
#   1. role absent                                    -> create + set password
#   2/3. role exists, target DB absent or empty        -> synchronize password
#   4. role exists, target DB has restored content     -> refuse (untouched)
#   5. role exists and is a PostgreSQL superuser        -> refuse (untouched)
if [ "$ROLE_EXISTS" != "1" ]; then
  do_or_plan_redacted \
    "create PostgreSQL login role '$DB_USER' (password from $ENV_FILE: $(redact "$DB_PASSWORD"))" \
    create_postgresql_role_with_password
elif [ "$ROLE_IS_SUPERUSER" = "1" ]; then
  log_error "Role '$DB_USER' already exists and is a PostgreSQL SUPERUSER -- this does not look like a role this restore tool ever created. Refusing to touch its password automatically; resolve manually (drop/rename it first if a fresh role really is intended)."
  [ "$RESTORE_MODE" = "apply" ] && exit 1
elif [ "$TARGET_DB_HAS_CONTENT" -eq 1 ]; then
  log_error "Role '$DB_USER' already exists and '$RESTORE_DB_NAME' already has $TARGET_DB_TABLE_COUNT restored table(s) -- leaving its password untouched (either a completed prior restore or an unrelated pre-existing installation; either way not safe to auto-resync). Re-run with --force-db only if you intend to overwrite its data; otherwise resolve manually."
  [ "$RESTORE_MODE" = "apply" ] && exit 1
else
  log_warn "Role '$DB_USER' already exists and '$RESTORE_DB_NAME' is absent or empty -- synchronizing its password to $ENV_FILE's value (safe: there is no restored content yet for a wrong password to protect)."
  do_or_plan_redacted \
    "synchronize existing PostgreSQL role '$DB_USER' password to $ENV_FILE's value (password: $(redact "$DB_PASSWORD"))" \
    set_postgresql_role_password
fi

# ---- 3. Database bootstrap ------------------------------------------------
# Encoding/locale made EXPLICIT here rather than relying on the cluster's
# own default matching -- docs/DISASTER_RECOVERY.md flagged this as a
# real gap ("Not explicitly forced by any CREATE DATABASE flag") on a
# target whose default locale differs from production's
# (en_US.UTF-8/libc). Deterministic per Phase 4's own mandate.
if [ "$DB_EXISTS" = "1" ]; then
  log_info "Database '$RESTORE_DB_NAME' already exists -- skipping CREATE DATABASE."
else
  do_or_plan sudo -u postgres psql -c "CREATE DATABASE ${RESTORE_DB_NAME} OWNER ${DB_USER} ENCODING 'UTF8' LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8' TEMPLATE template0"
fi
do_or_plan sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${RESTORE_DB_NAME} TO ${DB_USER}"

# ---- 4. Guard against overwriting real content ---------------------------
if [ "$RESTORE_MODE" = "apply" ]; then
  guard_db_overwrite "$RESTORE_DB_NAME" "$DB_USER" "$DB_HOST" "$DB_PORT"
fi

# ---- 5. pg_restore --------------------------------------------------------
# pg_restore needs a real seekable file for -Fc (custom format) -- unlike
# inspect_backup.sh's other checks, this cannot be streamed through a
# pipe. Extracted to a private temp dir, cleaned up on exit regardless of
# outcome (trap), never left lying around with a station's full database
# dump in it.
TMPDIR_DUMP="$(mktemp -d /tmp/isadoraair-restore-dbdump.XXXXXX)"
cleanup_dump() { rm -rf "$TMPDIR_DUMP"; }
trap cleanup_dump EXIT

if [ "$RESTORE_MODE" = "apply" ]; then
  log_apply "extracting database.dump to $TMPDIR_DUMP (temporary, removed on exit)"
  tar -xzO -f "$RESTORE_ARCHIVE" ./database.dump > "$TMPDIR_DUMP/database.dump"
  log_apply "pg_restore -h $DB_HOST -p $DB_PORT -U $DB_USER -d $RESTORE_DB_NAME --no-owner $TMPDIR_DUMP/database.dump"
  pg_restore -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$RESTORE_DB_NAME" --no-owner "$TMPDIR_DUMP/database.dump"
  log_info "pg_restore completed."

  # ---- 6. Verification (direct DB inspection -- the Python venv doesn't
  #         exist yet at this point in the restore order, see
  #         deploy/restore/README.md's dependency map, so this checks via
  #         psql directly rather than manage.py). ------------------------
  TABLE_COUNT=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$RESTORE_DB_NAME" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
  log_info "Restored database has $TABLE_COUNT table(s) in the public schema."
  if [ "$TABLE_COUNT" -eq 0 ]; then
    log_error "pg_restore reported success but the database has zero tables -- something is wrong. Investigate before proceeding."
    exit 1
  fi

  MIGRATIONS_EXISTS=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$RESTORE_DB_NAME" -tAc \
    "SELECT 1 FROM information_schema.tables WHERE table_schema = 'public' AND table_name = 'django_migrations'")
  if [ "$MIGRATIONS_EXISTS" = "1" ]; then
    APPLIED_COUNT=$(psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$RESTORE_DB_NAME" -tAc "SELECT count(*) FROM django_migrations")
    log_info "django_migrations table present with $APPLIED_COUNT applied migration row(s) -- migration state was preserved by the dump/restore, as expected (a restore against the exact recorded Git SHA should not need to run migrate)."
  else
    log_error "No django_migrations table found -- this does not look like a valid IsadoraAir database dump."
    exit 1
  fi
  restore_ledger_record "30-postgresql"
  log_info "30-postgresql: PASS"
else
  log_plan "extract database.dump to a temp file, then: pg_restore -h $DB_HOST -p $DB_PORT -U $DB_USER -d $RESTORE_DB_NAME --no-owner <dump>"
  log_plan "verify table count > 0 and django_migrations exists via direct psql query"
  log_info "30-postgresql: PLAN complete"
fi
