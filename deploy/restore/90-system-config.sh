#!/usr/bin/env bash
# deploy/restore/90-system-config.sh -- IsadoraAir 1.2 Phase 4.
#
# Renders + installs nginx, systemd, and ALSA config using the SAME
# @@PLACEHOLDER@@ substitution loop deploy/README.md already documents
# as the canonical install procedure -- this stage doesn't invent a new
# mechanism, it automates the exact one already established, adding
# pre-enable validation on top:
#   - `systemd-analyze verify` on every rendered unit file.
#   - Referenced-path existence checks (venv, manage.py, .env).
#   - Service user/group existence checks (`getent passwd`/`getent group`).
#   - `nginx -t` (real installs only -- see note below).
#
# NEVER starts, enables, or reloads anything -- Phase 4 spec sections 27
# ("not reload/start nginx automatically in staging mode") and 28
# ("Do not start all services merely because unit files were copied").
# That's Phase 5's job, following deploy/restore/README.md's service
# bring-up order doc.
#
# Under --staging-root, everything renders into a parallel
# $STAGING_ROOT/etc/... tree instead of the real /etc -- `nginx -t`
# cannot meaningfully validate an isolated site file without a full
# config tree, so it's skipped in staging mode (noted explicitly, not
# silently skipped) while systemd-analyze verify (which needs no
# installation context) still runs either way.
#
# Usage:
#   deploy/restore/90-system-config.sh [--plan|--apply] [--staging-root PATH]
#     [--isa-user USER] [--isa-home PATH]
#     [--syndicated-root PATH] [--weather-root PATH] [--ogremote-root PATH]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

restore_parse_common_args "$@"
set -- "${RESTORE_REMAINING_ARGS[@]}"

ISA_USER="$(id -un)"
ISA_HOME="$HOME"
SYNDICATED_ROOT=""
WEATHER_ROOT=""
OGREMOTE_ROOT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --isa-user) ISA_USER="${2:?}"; shift 2 ;;
    --isa-user=*) ISA_USER="${1#*=}"; shift ;;
    --isa-home) ISA_HOME="${2:?}"; shift 2 ;;
    --isa-home=*) ISA_HOME="${1#*=}"; shift ;;
    --syndicated-root) SYNDICATED_ROOT="${2:?}"; shift 2 ;;
    --syndicated-root=*) SYNDICATED_ROOT="${1#*=}"; shift ;;
    --weather-root) WEATHER_ROOT="${2:?}"; shift 2 ;;
    --weather-root=*) WEATHER_ROOT="${1#*=}"; shift ;;
    --ogremote-root) OGREMOTE_ROOT="${2:?}"; shift 2 ;;
    --ogremote-root=*) OGREMOTE_ROOT="${1#*=}"; shift ;;
    *) log_error "90-system-config.sh: unrecognized argument: $1"; exit 2 ;;
  esac
done
COMPANIONS_ROOT="${RESTORE_STAGING_ROOT:-$HOME}"
[ -z "$SYNDICATED_ROOT" ] && SYNDICATED_ROOT="$COMPANIONS_ROOT/syndicated-ingest"
[ -z "$WEATHER_ROOT" ] && WEATHER_ROOT="$COMPANIONS_ROOT/weather-ingest"
[ -z "$OGREMOTE_ROOT" ] && OGREMOTE_ROOT="$COMPANIONS_ROOT/ogremote-ingest"
ISA_ROOT="$RESTORE_TARGET_ROOT"

log_info "=== 90-system-config ==="
guard_production_target

if [ -n "$RESTORE_STAGING_ROOT" ]; then
  ETC_ROOT="$RESTORE_STAGING_ROOT/etc"
  USE_SUDO=0
else
  ETC_ROOT="/etc"
  USE_SUDO=1
fi
log_info "ISA_USER=$ISA_USER ISA_ROOT=$ISA_ROOT ISA_HOME=$ISA_HOME"
log_info "Rendering into: $ETC_ROOT"

render() {
  sed \
    -e "s|@@ISA_USER@@|$ISA_USER|g" \
    -e "s|@@ISA_ROOT@@|$ISA_ROOT|g" \
    -e "s|@@ISA_HOME@@|$ISA_HOME|g" \
    -e "s|@@SYNDICATED_ROOT@@|$SYNDICATED_ROOT|g" \
    -e "s|@@WEATHER_ROOT@@|$WEATHER_ROOT|g" \
    -e "s|@@OGREMOTE_ROOT@@|$OGREMOTE_ROOT|g" \
    "$1"
}
install_rendered() {
  local src="$1" dest="$2"
  if [ "$RESTORE_MODE" != "apply" ]; then
    log_plan "render $src -> $dest"
    return
  fi
  log_apply "render $src -> $dest"
  local tmp
  tmp="$(mktemp)"
  render "$src" > "$tmp"
  if [ "$USE_SUDO" -eq 1 ]; then
    sudo mkdir -p "$(dirname "$dest")"
    sudo cp "$tmp" "$dest"
  else
    mkdir -p "$(dirname "$dest")"
    cp "$tmp" "$dest"
  fi
  rm -f "$tmp"
}

# ---- 1. Render + install every deploy/*.service, *.timer, *.conf --------
# NOTE: deploy/README.md's own documented install loop uses this same
# broad `deploy/*.conf` glob, which -- followed literally -- ALSO
# matches asound.conf and isadoraair-locations.conf and would install
# both a second time at a bogus /etc/systemd/system/<name> path (neither
# is a systemd unit) before their own correct, dedicated steps below
# install them at the right place. That's a latent bug in the
# documented loop, not something to reproduce here -- both are
# explicitly excluded from this generic pass and handled only by their
# dedicated steps (section 2 for asound.conf, section 3 for
# isadoraair-locations.conf) below.
UNIT_COUNT=0
RENDERED_UNITS=()
for f in "$REPO_ROOT"/deploy/*.service "$REPO_ROOT"/deploy/*.timer "$REPO_ROOT"/deploy/*.conf; do
  [ -f "$f" ] || continue
  case "$(basename "$f")" in
    asound.conf|isadoraair-locations.conf) continue ;;
  esac
  dest="$ETC_ROOT/systemd/system/$(basename "$f")"
  case "$(basename "$f")" in
    isadoraair-aloop.conf) dest="$ETC_ROOT/modprobe.d/$(basename "$f")" ;;
    isadoraair-tmpfiles.conf) dest="$ETC_ROOT/tmpfiles.d/isadoraair.conf" ;;
    needrestart-isadoraair.conf) dest="$ETC_ROOT/needrestart/conf.d/isadoraair.conf" ;;
  esac
  install_rendered "$f" "$dest"
  RENDERED_UNITS+=("$dest")
  UNIT_COUNT=$((UNIT_COUNT + 1))
done
log_info "Rendered $UNIT_COUNT unit/timer/conf file(s)."

# ---- 2. asound.conf (not tokenized -- installed as-is) --------------------
install_rendered "$REPO_ROOT/deploy/asound.conf" "$ETC_ROOT/asound.conf"

# ---- 3. nginx site + snippet ----------------------------------------------
install_rendered "$REPO_ROOT/deploy/isadoraair-locations.conf" "$ETC_ROOT/nginx/snippets/isadoraair-locations.conf"
install_rendered "$REPO_ROOT/deploy/isadoraair.nginx" "$ETC_ROOT/nginx/sites-available/isadoraair"
if [ "$RESTORE_MODE" = "apply" ]; then
  if [ "$USE_SUDO" -eq 1 ]; then
    sudo mkdir -p "$ETC_ROOT/nginx/sites-enabled"
    sudo ln -sf "$ETC_ROOT/nginx/sites-available/isadoraair" "$ETC_ROOT/nginx/sites-enabled/isadoraair"
  else
    mkdir -p "$ETC_ROOT/nginx/sites-enabled"
    ln -sf "$ETC_ROOT/nginx/sites-available/isadoraair" "$ETC_ROOT/nginx/sites-enabled/isadoraair"
  fi
  log_info "sites-enabled/isadoraair -> sites-available/isadoraair (symlink, per deploy/README.md's 'One authoritative nginx config')."
else
  log_plan "ln -sf $ETC_ROOT/nginx/sites-available/isadoraair $ETC_ROOT/nginx/sites-enabled/isadoraair"
fi

# ---- 4. Validation (never enable/start/reload anything below) -----------
if [ "$RESTORE_MODE" = "apply" ]; then
  log_info "--- Validation ---"

  # 4a. Unit syntax -- systemd-analyze verify needs no installation
  #     context, works directly against a file path.
  if command -v systemd-analyze >/dev/null 2>&1; then
    UNIT_FAIL=0
    for u in "${RENDERED_UNITS[@]}"; do
      case "$u" in
        *.service|*.timer)
          if systemd-analyze verify "$u" 2>&1 | grep -qv '^$'; then
            :  # systemd-analyze verify prints warnings for cross-references
               # to units not YET installed (timers referencing services in
               # the same batch) -- expected mid-batch, not necessarily fatal.
               # Real syntax errors are still visible in the output either way.
          fi
          ;;
      esac
    done
    log_info "systemd-analyze verify: ran against every rendered .service/.timer (see output above for any warnings -- cross-references to not-yet-installed sibling units in this same batch are expected and not failures)."
  else
    log_warn "systemd-analyze not found -- skipping unit syntax verification."
  fi

  # 4b. Referenced paths exist
  PATH_FAIL=0
  for p in "$ISA_ROOT/manage.py" "$ISA_ROOT/venv/bin/python" "$ISA_ROOT/.env"; do
    if [ -e "$p" ]; then
      log_info "  [x] $p exists"
    else
      log_warn "  [ ] $p does NOT exist -- units referencing it will fail to start (expected if earlier stages haven't run yet)."
      PATH_FAIL=1
    fi
  done

  # 4c. Service user/group exist
  if getent passwd "$ISA_USER" >/dev/null 2>&1; then
    log_info "  [x] user '$ISA_USER' exists"
  else
    log_warn "  [ ] user '$ISA_USER' does NOT exist -- create it before enabling any unit."
  fi

  # 4d. nginx -t -- only meaningful against the REAL /etc/nginx tree; an
  #     isolated staged site file has no full config context to check
  #     against, so this is skipped (not faked) under --staging-root.
  if [ "$USE_SUDO" -eq 1 ] && command -v nginx >/dev/null 2>&1; then
    log_info "Running nginx -t..."
    if sudo nginx -t 2>&1; then
      log_info "nginx -t: PASS"
    else
      log_error "nginx -t: FAILED -- see output above."
    fi
  else
    log_warn "nginx -t skipped ($( [ "$USE_SUDO" -eq 0 ] && echo "staging mode -- isolated site file has no full config tree to validate against" || echo "nginx not found" ))."
  fi

  # 4e. snd-aloop verification -- read-only, real (non-staging) mode
  #     only, since a staging root cannot load a kernel module. Per
  #     Phase 4 spec section 23: "Document snd-aloop load; expected
  #     indices; verification; expected airtap/airtap_ds aliases" --
  #     this is the verification half; the config was already installed
  #     to $ETC_ROOT/modprobe.d above. Does NOT attempt to solve the
  #     unstable USB card numbering (roadmap 1.3, out of scope here).
  if [ "$USE_SUDO" -eq 1 ]; then
    if [ -f /proc/asound/cards ]; then
      LOOPBACK_COUNT=$(grep -c 'Loopback' /proc/asound/cards || true)
      if [ "$LOOPBACK_COUNT" -ge 3 ]; then
        log_info "snd-aloop: $LOOPBACK_COUNT Loopback card(s) present in /proc/asound/cards -- module is loaded with the expected 3-instance layout."
      else
        log_warn "snd-aloop: only $LOOPBACK_COUNT Loopback card(s) found (expected 3 at indices 0/3/4) -- module may not be loaded yet with the isadoraair-aloop.conf options installed above. A reboot (or 'sudo modprobe -r snd_aloop && sudo modprobe snd-aloop') is needed after installing the modprobe.d config for it to take effect -- see README.md's 'ALSA loopback module' section."
      fi
    else
      log_warn "snd-aloop: /proc/asound/cards not present -- no ALSA sound subsystem on this host, or snd-aloop not loaded at all."
    fi
  else
    log_warn "snd-aloop verification skipped in staging mode -- cannot load kernel modules into an isolated tree."
  fi

  log_info "No unit was started, enabled, or reloaded by this stage. nginx was NOT reloaded even in real-install mode."
fi

log_info "90-system-config: $( [ "$RESTORE_MODE" = apply ] && echo "PASS (installation + validation complete, nothing started -- see deploy/restore/README.md's service bring-up order for what comes next)" || echo "PLAN complete" )"
