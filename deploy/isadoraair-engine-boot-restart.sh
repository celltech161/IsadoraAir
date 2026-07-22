#!/bin/bash
# /usr/local/bin/isadoraair-engine-boot-restart.sh
#
# Fired ~50s after boot by isadoraair-engine-boot-restart.timer.
#
# Original design (2026-07): unconditionally restart the engine at
# +50s to work around an intermittent cold-boot race where the first
# start of isadoraair-engine.service came up with silent decks. That
# was a shotgun approach: cold boots that came up healthy (which
# turned out to be most of them, once other fixes landed) still got
# 5-10s of dead air and a track advance during the restart.
#
# This script makes the restart CONDITIONAL: read the encoders'
# silence-detector state file (written by liquidsoap's blank.detect
# callback via write_silence_state, see
# encoders/services/encoder_manager.py's build_liquidsoap_script)
# and only restart if the encoders are reporting silence at this
# moment. If audio is flowing cleanly, the boot was healthy this
# time and there is no need to interrupt the on-air path.
#
# JSON shape (compact, no spaces):
#   {"is_blank":true|false,"timestamp":<epoch>,"since":<epoch>}

STATE_FILE=$(ls /run/isadoraair/liquidsoap_silence_*.json 2>/dev/null | head -1)

if [ -z "$STATE_FILE" ]; then
    # File missing entirely = encoders haven't come up yet or a slug
    # mismatch on DEFAULT_INPUT_DEVICE (see project notes). Either
    # way something is definitively off; fall back to the original
    # unconditional-restart behavior rather than assume things are OK.
    logger -t isadoraair-boot-restart "no liquidsoap silence state file found under /run/isadoraair/; restarting engine defensively"
    exec /usr/bin/systemctl restart isadoraair-engine.service
fi

if grep -qE '"is_blank"[[:space:]]*:[[:space:]]*false' "$STATE_FILE"; then
    logger -t isadoraair-boot-restart "audio flowing (is_blank=false in $STATE_FILE); skipping engine restart"
    exit 0
fi

logger -t isadoraair-boot-restart "silence detected (is_blank=true in $STATE_FILE); restarting engine"
exec /usr/bin/systemctl restart isadoraair-engine.service
