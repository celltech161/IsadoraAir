#!/usr/bin/env bash
# deploy/restore/60-python.sh -- IsadoraAir 1.2 Phase 4.
#
# Creates the IsadoraAir Python venv exactly per README.md's documented
# procedure (step 5) -- Ubuntu 26.04's own Python 3.14,
# --system-site-packages preserved (so PyGObject/gi, installed as an OS
# package by 10-packages.sh, is visible inside the venv), then
# `requirements.txt` installed as committed -- never a blind `pip
# freeze` capture, which would pull in whatever happens to be resolved
# on THIS box rather than the pinned, reviewed manifest.
#
# Runs two safe, read-only verification checks afterward:
#   1. `import gi; ... Gst.init(None)` -- confirms the venv can actually
#      see the system GStreamer bindings (the #1 way --system-site-packages
#      silently fails to matter is forgetting the flag or a venv module
#      shadowing the system one).
#   2. `manage.py check` -- Django's own system check framework. Requires
#      .env (stage 20) and a reachable database (stage 30) to have
#      already run.
#
# r0048: a MODERN target (weather_ingest/requirements.txt present in
# the checked-out source -- see lib.sh's restore_target_has_intree_weather)
# additionally gets its own independently reproducible weather-ingest
# venv, built the same idempotent way as the main venv above but
# WITHOUT --system-site-packages (the standalone companion's own
# isolation semantics, preserved verbatim -- weather-ingest has no
# GStreamer/PyGObject dependency, confirmed by inspecting its committed
# requirements.txt/README.md, so there is no reason to weaken its
# isolation just because it now lives inside this repository). A LEGACY
# target (no in-tree weather source) logs that weather runtime
# provisioning is deferred to Stage 80's standalone companion path --
# never treated as this stage's own failure/corruption.
#
# Usage:
#   deploy/restore/60-python.sh [--plan|--apply] [--staging-root PATH]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
source "$SCRIPT_DIR/lib.sh"

restore_parse_common_args "$@"

log_info "=== 60-python ==="
guard_production_target
require_cmd python3

if [ ! -f "$RESTORE_TARGET_ROOT/manage.py" ]; then
  log_error "$RESTORE_TARGET_ROOT/manage.py not found -- run 20-application.sh first."
  exit 1
fi
REQUIREMENTS="$RESTORE_TARGET_ROOT/requirements.txt"
if [ ! -f "$REQUIREMENTS" ]; then
  log_error "$REQUIREMENTS not found."
  exit 1
fi

VENV_DIR="$RESTORE_TARGET_ROOT/venv"
PY_VERSION=$(python3 --version 2>&1)
log_info "System Python: $PY_VERSION"

if [ -d "$VENV_DIR" ]; then
  log_info "$VENV_DIR already exists -- verifying rather than recreating (idempotent). Delete it manually first if a from-scratch rebuild is actually what's wanted."
else
  do_or_plan python3 -m venv "$VENV_DIR" --system-site-packages
fi

do_or_plan "$VENV_DIR/bin/pip" install --upgrade pip
do_or_plan "$VENV_DIR/bin/pip" install -r "$REQUIREMENTS"

if [ "$RESTORE_MODE" = "apply" ]; then
  log_info "Verifying GStreamer/PyGObject visibility inside the venv..."
  if "$VENV_DIR/bin/python" -c "
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)
print('Gst version:', '.'.join(str(x) for x in Gst.version()))
"; then
    log_info "gi/GStreamer import: PASS"
  else
    log_error "gi/GStreamer import FAILED inside the venv -- check --system-site-packages was preserved and 10-packages.sh's AUDIO_GSTREAMER group is installed."
    exit 1
  fi

  ENV_FILE="$RESTORE_TARGET_ROOT/.env"
  if [ -f "$ENV_FILE" ]; then
    log_info "Running manage.py check..."
    if ( cd "$RESTORE_TARGET_ROOT" && "$VENV_DIR/bin/python" manage.py check ); then
      log_info "manage.py check: PASS"
    else
      log_error "manage.py check FAILED -- see output above. This usually means .env/database issues from earlier stages, not this stage itself."
      exit 1
    fi
  else
    log_warn "No .env at $ENV_FILE -- skipping manage.py check (run 20-application.sh first for a full verification)."
  fi
else
  log_plan "$VENV_DIR/bin/python -c \"import gi; ...; Gst.init(None)\""
  log_plan "cd $RESTORE_TARGET_ROOT && $VENV_DIR/bin/python manage.py check"
fi

# ---------------------------------------------------------------------
# r0048: in-tree weather-ingest venv (modern target only).
# ---------------------------------------------------------------------
if restore_target_has_intree_weather "$RESTORE_TARGET_ROOT"; then
  WEATHER_ROOT="$RESTORE_TARGET_ROOT/weather_ingest"
  WEATHER_REQUIREMENTS="$WEATHER_ROOT/requirements.txt"
  WEATHER_VENV_DIR="$WEATHER_ROOT/venv"
  log_info "In-tree weather source detected ($WEATHER_ROOT) -- provisioning its isolated venv."

  if [ -d "$WEATHER_VENV_DIR" ]; then
    log_info "$WEATHER_VENV_DIR already exists -- verifying rather than recreating (idempotent). Delete it manually first if a from-scratch rebuild is actually what's wanted."
  else
    do_or_plan python3 -m venv "$WEATHER_VENV_DIR"
  fi

  do_or_plan "$WEATHER_VENV_DIR/bin/pip" install --upgrade pip
  do_or_plan "$WEATHER_VENV_DIR/bin/pip" install -r "$WEATHER_REQUIREMENTS"

  if [ "$RESTORE_MODE" = "apply" ]; then
    log_info "Verifying the weather venv's own interpreter is usable..."
    if "$WEATHER_VENV_DIR/bin/python" -c "import requests; print('requests version:', requests.__version__)"; then
      log_info "weather venv interpreter check: PASS"
    else
      log_error "weather venv interpreter check FAILED -- see output above."
      exit 1
    fi
  else
    log_plan "$WEATHER_VENV_DIR/bin/python -c \"import requests; ...\""
  fi
else
  log_info "No in-tree weather source (weather_ingest/requirements.txt absent) -- this is a legacy target. Weather runtime provisioning is deferred to Stage 80's standalone companion path, not this stage's concern."
fi

if [ "$RESTORE_MODE" = "apply" ]; then
  restore_ledger_record "60-python"
  log_info "60-python: PASS"
else
  log_info "60-python: PLAN complete"
fi
