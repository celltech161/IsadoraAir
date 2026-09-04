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

# createuser --pwprompt is PostgreSQL's purpose-built non-echoing path for
# assigning a new role password. Feeding its two prompts over stdin keeps the
# password out of both the process argument list and SQL text that psql could
# reproduce in an error message. Never replace this with `psql -c "...PASSWORD
# '$DB_PASSWORD'"`: do_or_plan logs its arguments, and psql errors may echo the
# submitted SQL to stderr.
create_postgresql_role_with_password() {
  printf '%s\n%s\n' "$DB_PASSWORD" "$DB_PASSWORD" |
    sudo -u postgres createuser --pwprompt --no-password "$DB_USER"
}

# ---- 1. Role bootstrap ---------------------------------------------------
ROLE_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname = '${DB_USER}'" 2>/dev/null || true)
if [ "$ROLE_EXISTS" = "1" ]; then
  log_info "Role '$DB_USER' already exists -- skipping CREATE USER."
else
  do_or_plan_redacted \
    "create PostgreSQL login role '$DB_USER' with createuser --pwprompt (password from $ENV_FILE: $(redact "$DB_PASSWORD"))" \
    create_postgresql_role_with_password
fi

# ---- 2. Database bootstrap ------------------------------------------------
# Encoding/locale made EXPLICIT here rather than relying on the cluster's
# own default matching -- docs/DISASTER_RECOVERY.md flagged this as a
# real gap ("Not explicitly forced by any CREATE DATABASE flag") on a
# target whose default locale differs from production's
# (en_US.UTF-8/libc). Deterministic per Phase 4's own mandate.
DB_EXISTS=$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = '${RESTORE_DB_NAME}'" 2>/dev/null || true)
if [ "$DB_EXISTS" = "1" ]; then
  log_info "Database '$RESTORE_DB_NAME' already exists -- skipping CREATE DATABASE."
else
  do_or_plan sudo -u postgres psql -c "CREATE DATABASE ${RESTORE_DB_NAME} OWNER ${DB_USER} ENCODING 'UTF8' LC_COLLATE 'en_US.UTF-8' LC_CTYPE 'en_US.UTF-8' TEMPLATE template0"
fi
do_or_plan sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ${RESTORE_DB_NAME} TO ${DB_USER}"

# ---- 3. Guard against overwriting real content ---------------------------
if [ "$RESTORE_MODE" = "apply" ]; then
  guard_db_overwrite "$RESTORE_DB_NAME" "$DB_USER" "$DB_HOST" "$DB_PORT"
fi

# ---- 4. pg_restore --------------------------------------------------------
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

  # ---- 5. Verification (direct DB inspection -- the Python venv doesn't
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
  log_info "30-postgresql: PASS"
else
  log_plan "extract database.dump to a temp file, then: pg_restore -h $DB_HOST -p $DB_PORT -U $DB_USER -d $RESTORE_DB_NAME --no-owner <dump>"
  log_plan "verify table count > 0 and django_migrations exists via direct psql query"
  log_info "30-postgresql: PLAN complete"
fi
