#!/usr/bin/env bash
# Canonical real-machine disaster-recovery entrypoint.
#
# The physical P0 1.2 bare-metal drill proved that running restore.sh itself
# under sudo violates the restore stages' identity contract: .env/venvs became
# root-owned, companion repositories landed under /root, and Stage 90 rendered
# /root companion paths.  Individual stages already use sudo only for the
# privileged writes they own.  Therefore a real bare-metal restore must be
# orchestrated by the intended ordinary operator/service account.
#
# This wrapper enforces that boundary, runs the mature stage orchestrator
# unchanged, performs the one machine-local post-restore transformation the
# drill also exposed (ALLOWED_HOSTS/CSRF_TRUSTED_ORIGINS rebinding), and then
# reruns Stage 95 so the final PASS is after rebinding, not before it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

ORIGINAL_ARGS=("$@")
ISA_USER="$(id -un)"
for ((i=0; i<${#ORIGINAL_ARGS[@]}; i++)); do
  case "${ORIGINAL_ARGS[$i]}" in
    --isa-user)
      if [ $((i + 1)) -ge ${#ORIGINAL_ARGS[@]} ]; then
        log_error "--isa-user needs a value"
        exit 2
      fi
      ISA_USER="${ORIGINAL_ARGS[$((i + 1))]}"
      ;;
    --isa-user=*) ISA_USER="${ORIGINAL_ARGS[$i]#*=}" ;;
  esac
done

restore_parse_common_args "${ORIGINAL_ARGS[@]}"

if [ "$RESTORE_MODE" != "apply" ]; then
  log_error "bare_metal_restore.sh requires --apply. Use restore.sh directly for --plan/staging work."
  exit 2
fi
if [ -n "$RESTORE_STAGING_ROOT" ]; then
  log_error "bare_metal_restore.sh is only for the real booted target; --staging-root is not accepted."
  exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
  log_error "Do NOT run the bare-metal restore under sudo/root. Run it as the intended operator account ('$ISA_USER'); the restore stages invoke sudo internally only where required."
  exit 1
fi
if [ "$(id -un)" != "$ISA_USER" ]; then
  log_error "Bare-metal restore caller mismatch: running as '$(id -un)' but --isa-user is '$ISA_USER'. Run the wrapper as the intended service/operator account."
  exit 1
fi
if [ -z "$RESTORE_ARCHIVE" ] || [ ! -f "$RESTORE_ARCHIVE" ]; then
  log_error "A valid --archive is required for bare-metal recovery."
  exit 1
fi

log_info "Bare-metal identity boundary PASS: orchestrator is running as $ISA_USER (uid $(id -u)), not root."
"$SCRIPT_DIR/restore.sh" "${ORIGINAL_ARGS[@]}"

ENV_PATH="$RESTORE_TARGET_ROOT/.env"
if [ ! -f "$ENV_PATH" ]; then
  log_error "Restore completed but $ENV_PATH is missing; cannot perform host-network rebinding."
  exit 1
fi

log_info "=== Bare-metal host-network rebinding ==="
python3 "$SCRIPT_DIR/rebind_host_network.py" --env "$ENV_PATH" --apply

# Stage 95 already ran once inside restore.sh. Re-run it after the .env
# machine-identity rewrite so the final acceptance PASS refers to the state
# that will actually be used for service bring-up.
VALIDATE_ARGS=(
  --archive "$RESTORE_ARCHIVE"
  --apply
  --force-production-target
  --db-name "$RESTORE_DB_NAME"
  --isa-user "$ISA_USER"
)
log_info "=== Post-rebind final validation ==="
"$SCRIPT_DIR/95-validate.sh" "${VALIDATE_ARGS[@]}"

log_info "Bare-metal restore PASS: operator identity preserved and host-local Django network identity rebound before service bring-up."
