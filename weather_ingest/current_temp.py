#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# WX Current Temperature Announcer
# --------------------------------
# Builds a short on-air weather hit (current temperature plus any active
# weather alert) via the canonical shared IsadoraAir TTS CLI (see
# lib/voices.py). Used as a between-songs filler that also delivers
# weather-alert context in its short blurb.
#
# Ported from /home/jreed/wx_scripts/current_temp.py (originally on the
# kogr-sc box, delivered via a Samba/webroot copy that NextKast polled
# as a remote URL stream). Delivered here via lib/delivery.py straight
# into the existing WxTemp category/current_temp.mp3 filename instead --
# the engine reads the file fresh off local disk at play time (same
# proven pattern as every ported syndicated show), so there's no need
# for the old urgent_mix-append trick that worked around NextKast not
# noticing freshly dropped-in files. Severe watch/warning alerts get
# their own dedicated, near-instant delivery via wx_alert.py's
# insert_urgent instead -- this script's short blurb (drawn from
# warn.txt) now mentions an active alert on every routine airing for as
# long as it stays active, rather than being suppressed to avoid
# duplicating a persistently-appended urgent clip (that concept doesn't
# exist in this design; wx_alert.py's clip only plays once, at
# insertion, not on every subsequent temp/forecast segment).
#
# Two voice variants are supported via --voice:
#   day   -> Claira Sky (female voice)
#   night -> Max Weatherly (male voice)
#   auto  -> consults WeatherConfig.voice_schedule for the current hour
#
# Cron + immediate-trigger usage:
#   /path/to/current_temp.py --voice auto    # cron every 15 min
# When update_local_wx_data.py detects a change in the active alert
# fingerprint, it fires this script with --voice auto immediately so the
# next song boundary plays the updated weather hit. A lockfile prevents
# a cron-driven run from racing a trigger-driven run.
#
# Author: Justin Reed

import argparse
import fcntl
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from delivery import deliver
from notify import notify
import voices
from voices import VoiceResolutionError, resolve_voice
from wxconfig import load_weather_config, resolve_weather_data_dir

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [current_temp] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------- CONFIGURATION ----------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = resolve_weather_data_dir()

DATA_FILE = DATA_DIR / "latest_weather.json"
SMOOTHED_WIND_FILE = DATA_DIR / "smoothed_wind.json"
ALERT_DESCRIPTION_FILE = DATA_DIR / "warn.txt"

TMP_DIR = "/tmp/syndicated-current-temp"
OUTPUT_WAV = os.path.join(TMP_DIR, "current_temp.wav")
OUTPUT_MP3 = os.path.join(TMP_DIR, "current_temp.mp3")

CATEGORY_CODE = "WxTemp"
DEST_FILENAME = "current_temp.mp3"

# Best-effort with LOCK_NB: if a run is in progress, the second
# invocation exits cleanly. The trigger from update_local_wx_data.py
# treats this as success -- if a run is already in flight, that run
# will pick up the new alert state from warn.txt (written before the
# trigger fires).
LOCK_FILE = "/tmp/syndicated-current-temp.lock"

ID3_ARTIST = "Oak Grove Radio"

# ---------------- HELPERS ----------------

def convert_to_mp3(wav_file, mp3_file, id3_title):
    if not os.path.exists(wav_file):
        log.error("WAV file missing: %s", wav_file)
        return False
    try:
        os.remove(mp3_file)
    except FileNotFoundError:
        pass
    except OSError as e:
        log.error("Could not clear stale MP3 at %s: %s", mp3_file, e)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-hide_banner", "-loglevel", "error",
                "-i", wav_file,
                # Piper's own audio starts right at sample 0 with no lead-in,
                # which reads as clipped on air -- 1s of silence up front
                # gives the on-air chain room to fade/cue in cleanly.
                "-af", "adelay=1000:all=1",
                "-ar", "44100",
                "-ac", "2",
                "-codec:a", "libmp3lame",
                "-b:a", "320k",
                "-metadata", f"artist={ID3_ARTIST}",
                "-metadata", f"title={id3_title}",
                mp3_file
            ],
            check=True,
        )
        log.info("Converted WAV to MP3 (44.1 kHz, stereo, 320 kbps): %s", mp3_file)
        return True
    except subprocess.CalledProcessError as e:
        log.error("FFmpeg conversion failed: %s", e)
        return False


def load_weather():
    if not os.path.exists(DATA_FILE):
        return None
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def load_smoothed_wind():
    if not os.path.exists(SMOOTHED_WIND_FILE):
        return {}
    with open(SMOOTHED_WIND_FILE, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}


def get_alert_summary():
    """Comma-joined string of active alerts, drawn straight from
    warn.txt. Collapses duplicate flood warnings (NWS often issues
    several for different waterways in the same area)."""
    if not os.path.exists(ALERT_DESCRIPTION_FILE):
        return ""
    with open(ALERT_DESCRIPTION_FILE, "r") as f:
        content = f.read().strip()
    if not content:
        return ""

    alerts = [a.strip() for a in re.split(r"[,\n]+", content) if a.strip()]
    seen = set()
    filtered = []
    for alert in alerts:
        alert_lower = alert.lower()
        if "flood warning" in alert_lower:
            if "flood warning" not in seen:
                filtered.append(alert)
                seen.add("flood warning")
        elif alert not in seen:
            filtered.append(alert)
            seen.add(alert)

    # Strip trailing periods before joining -- warn.txt entries often
    # end with "PM." and joining with a comma directly would produce
    # awkward ". ," runs. ", and, " reads as a natural conjunction
    # between multiple alerts.
    filtered = [a.rstrip(".").rstrip() for a in filtered]
    return ", and, ".join(filtered)


def calculate_feels_like(temp_f, humidity, wind_mph):
    """Return NWS-spec apparent temperature: heat index above 80F,
    wind chill at/below 50F with wind >= 3 mph, otherwise the raw air
    temp. Kept identical to weather-ingest/update_local_wx_data.py's
    version so the spoken temp and the RDS temp always agree. When HI
    rounds equal to actual temp, build_announcement's phrase gate
    (`feels_like != round(temp_f)`) cleanly drops the "but it feels
    like" sentence -- no need to gate on humidity here."""
    try:
        temp_f = float(temp_f)
        humidity = float(humidity)
        wind_mph = float(wind_mph)
    except (TypeError, ValueError):
        return round(temp_f)

    if temp_f >= 80:
        # NWS Rothfusz regression -- the base formula.
        hi = (-42.379 + 2.04901523 * temp_f + 10.14333127 * humidity
              - 0.22475541 * temp_f * humidity - 0.00683783 * temp_f ** 2
              - 0.05481717 * humidity ** 2 + 0.00122874 * temp_f ** 2 * humidity
              + 0.00085282 * temp_f * humidity ** 2 - 0.00000199 * temp_f ** 2 * humidity ** 2)
        # Low-humidity correction (dry heat overestimates otherwise).
        if humidity < 13 and 80 <= temp_f <= 112:
            hi -= ((13 - humidity) / 4.0) * math.sqrt((17 - abs(temp_f - 95)) / 17.0)
        # High-humidity correction (muggy at mid-80s underestimates).
        elif humidity > 85 and 80 <= temp_f <= 87:
            hi += ((humidity - 85) / 10.0) * ((87 - temp_f) / 5.0)
        return round(hi)

    if temp_f <= 50 and wind_mph >= 3:
        wc = (35.74 + 0.6215 * temp_f - 35.75 * wind_mph ** 0.16 + 0.4275 * temp_f * wind_mph ** 0.16)
        return round(wc)

    return round(temp_f)


def build_announcement(data):
    """Wind is used only as input to feels_like; nothing about wind
    appears in the spoken output, by design -- this is a quick
    temperature hit, not a full weather report."""
    temp_f = data.get("tempf", "unknown")
    humidity = data.get("humidity", "unknown")

    smoothed = load_smoothed_wind()
    wind_mph = smoothed.get("speed")
    if wind_mph is None:
        log.info("Smoothed wind missing - falling back to raw wind data for feels_like input.")
        wind_mph = data.get("windspeedmph", "unknown")

    try:
        temp_f = float(temp_f)
        humidity = float(humidity)
        wind_mph = float(wind_mph)
    except (TypeError, ValueError):
        return "Weather data is incomplete or invalid."

    feels_like = calculate_feels_like(temp_f, humidity, wind_mph)
    if feels_like != round(temp_f):
        temp_string = f"It's {round(temp_f)} degrees, but it feels like {feels_like}."
    else:
        temp_string = f"It's {round(temp_f)} degrees."

    alert_text = get_alert_summary()
    alert_intro = " Weather alert: " if alert_text else ""

    return f"{temp_string}{alert_intro}{alert_text}"


def generate_wav_with_piper(text, voice):
    """Preserves the original name/signature for the caller below;
    synthesis itself is voices.synthesize()'s job, via the canonical
    shared TTS CLI -- see lib/voices.py."""
    ok = voices.synthesize(text, OUTPUT_WAV, voice)
    if ok:
        log.info("WAV file generated at: %s", OUTPUT_WAV)
    return ok


# ---------------- MAIN ----------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate the Oak Grove Radio current-temperature announcement.",
    )
    parser.add_argument(
        "--voice",
        required=True,
        help="Persona slot key, or auto to use WeatherConfig.voice_schedule for the current hour.",
    )
    return parser.parse_args()


def main():
    """Returns True on success, False on any real synthesis-generation
    failure (config missing, slot/persona/logical-voice unresolvable,
    canonical CLI failure, MP3 conversion failure, delivery failure).
    A lock-contention no-op (another instance already running) is
    treated as a benign, non-failing exit -- see __main__ below."""
    args = parse_args()

    try:
        lock_fd = open(LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log.info("Another instance of current_temp.py is already running. Exiting.")
        return True

    try:
        try:
            cfg = load_weather_config()
            voice_key, voice = resolve_voice(cfg, args.voice)
        except VoiceResolutionError as e:
            log.error("Voice resolution failed: %s", e)
            notify("current_temp FAILED", f"Voice resolution failed: {e}")
            return False
        if args.voice == "auto":
            log.info("Voice auto-selected: %s", voice_key)

        id3_title = f"Current Temp ({voice['name']})"

        log.info("=== WX Temperature Announcer Started (voice=%s) ===", voice_key)
        data = load_weather()
        if not data:
            log.error("No weather data found at %s", DATA_FILE)
            return False

        announcement = build_announcement(data)
        log.info("Announcement: %s", announcement)

        synth_result = generate_wav_with_piper(announcement, voice)
        if not synth_result:
            notify(
                "current_temp FAILED",
                f"Synthesis failed ({synth_result.reason}).\nAnnouncement: {announcement}",
            )
            return False

        if not convert_to_mp3(OUTPUT_WAV, OUTPUT_MP3, id3_title):
            notify("current_temp FAILED", "MP3 conversion failed.")
            return False

        try:
            source_age_seconds = None
            try:
                source_age_seconds = time.time() - os.path.getmtime(DATA_FILE)
            except OSError:
                pass  # provenance is best-effort; publish proceeds either way
            dest = deliver(
                OUTPUT_MP3, CATEGORY_CODE, DEST_FILENAME,
                producer="current_temp.py", voice=voice["name"],
                source_kind="derived_local", source_age_seconds=source_age_seconds,
            )
            log.info("Delivered and synced: %s", dest)
        except Exception as e:
            log.error("Delivery failed: %s", e)
            notify("current_temp FAILED", f"Delivery failed: {e}")
            return False

        log.info("=== Completed successfully ===")
        return True

    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
