#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Watch/Warning alert beep.
#
# Ported from kogr-sc's arrangement: update_local_wx_data.py wrote
# /var/www/media/wx/alert.txt (present = alert active, absent = clear)
# for a separate box running "RDS magic" to poll over HTTP and, when
# active, play a beep file via VLC into a second StereoTool input.
# Consolidated onto one box, that middleman is gone -- this script reads
# the same alert.txt (now local, no HTTP needed -- see ALERT_STATUS in
# update_local_wx_data.py, unchanged) and, while an alert is active and
# due, requests the operator-selected FX Cart from IsadoraAir itself
# (WeatherConfig.alert_sound_cart) via the new `fire_fx_cart` management
# command bridge.
#
# This replaces the original direct ffmpeg -> ALSA loopback -> StereoTool
# Input 2 arrangement (2026-08 migration): that path bypassed the
# playback engine entirely, so the beep reached air through StereoTool
# but was never present in IsadoraAir's own monitored program bus --
# studio operators and remote DJs couldn't hear it in their monitor
# feeds. Firing an FX Cart instead routes the beep through the same
# fx_submix -> program_fx_mixer path every other cart uses, which is
# upstream of both the studio monitor and the remote-DJ monitor-return
# tee, so it's now audible everywhere the program bus is. It also drops
# this script's last StereoTool-specific dependency (a dedicated ALSA
# loopback device feeding a StereoTool second input) -- weather no
# longer needs to know StereoTool exists, part of migrating the whole
# weather suite into IsadoraAir over time.
#
# Engine-side firing (retrigger mode, polyphony cap, whether a sample
# is ultimately audible) is entirely the engine's concern -- this script
# only needs to know whether its fire request was successfully
# submitted, not whether audio ultimately emerged from GStreamer. For
# the repeat timer below, a successful command submission counts as a
# successful beep attempt, same as a successful ffmpeg run did before.
#
# Run on a tight, fixed cadence (systemd timer, ~30s) -- WeatherConfig
# is re-read fresh every run, so an admin edit to the interval, the
# master switch, or the selected cart takes effect on the very next
# tick, no restart needed.

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from wxconfig import load_weather_config, resolve_weather_data_dir  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = resolve_weather_data_dir()
ALERT_STATUS = DATA_DIR / "alert.txt"
STATE_FILE = DATA_DIR / "alert_beep_state.json"

# Same cross-venv-via-subprocess pattern as lib/wxconfig.py's own
# manage.py calls -- this venv has no Django installed, so firing the
# cart means shelling out to IsadoraAir's own venv/manage.py, same as
# every other Django-reaching call this script makes (indirectly, via
# load_weather_config()). Duplicated here rather than imported from
# wxconfig, matching how lib/delivery.py and lib/notify.py each keep
# their own copy rather than sharing one.
# See lib/delivery.py's identical constant for the full rationale
# (IsadoraAir 1.2 Phase 3 path audit, 2026-08-12) -- unchanged current
# behavior, just portable to a different install layout.
ISADORAAIR_DIR = Path(os.environ.get("ISADORAAIR_DIR", "/opt/isadoraair"))
ISADORAAIR_PYTHON = ISADORAAIR_DIR / "venv" / "bin" / "python"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [wx_alert_beep] %(message)s")
log = logging.getLogger(__name__)


def _load_state():
    if not STATE_FILE.exists():
        return {"last_played_iso": None}
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"last_played_iso": None}


def _save_state(state):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def _fire_cart(cart_id):
    """Submits cart_id to IsadoraAir's fire_fx_cart bridge command.
    Returns True on successful submission, False otherwise -- does not
    (and cannot, without new command-acknowledgement IPC that's out of
    scope here) confirm audio was actually heard."""
    cmd = [str(ISADORAAIR_PYTHON), "manage.py", "fire_fx_cart", str(cart_id)]
    log.info("Firing FX Cart %s via fire_fx_cart", cart_id)
    try:
        subprocess.run(cmd, cwd=str(ISADORAAIR_DIR), check=True,
                        capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as e:
        log.error("fire_fx_cart failed for cart %s: %s", cart_id, e.stderr.strip() if e.stderr else e)
        return False
    except subprocess.TimeoutExpired as e:
        log.error("fire_fx_cart timed out for cart %s: %s", cart_id, e)
        return False
    return True


def main():
    cfg = load_weather_config()

    if not cfg["alert_sound_enabled"]:
        # Master switch off -- also clear last-played state so a re-enable
        # doesn't inherit a stale timer and skip the very next active alert.
        _save_state({"last_played_iso": None})
        return

    if not ALERT_STATUS.exists():
        _save_state({"last_played_iso": None})
        return

    state = _load_state()
    interval_seconds = cfg["alert_sound_interval_seconds"]
    last_played_iso = state.get("last_played_iso")
    if last_played_iso:
        last_played = datetime.fromisoformat(last_played_iso)
        elapsed = (datetime.now(timezone.utc) - last_played).total_seconds()
        if elapsed < interval_seconds:
            return

    cart_id = cfg["alert_sound_cart_id"]
    if not cart_id:
        log.error("No alert_sound_cart configured in WeatherConfig; skipping beep.")
        return

    if not _fire_cart(cart_id):
        return

    _save_state({"last_played_iso": datetime.now(timezone.utc).isoformat()})


if __name__ == "__main__":
    main()
