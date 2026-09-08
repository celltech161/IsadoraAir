#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# WX Forecast Announcer
# ---------------------
# Generates a current-conditions snapshot plus an NWS-driven forecast,
# rendered as MP3 via the canonical shared IsadoraAir TTS CLI (see
# lib/voices.py) for broadcast.
#
# Ported from /home/jreed/wx_scripts/wx_forecast.py (originally on the
# kogr-sc box, delivered via a Samba/webroot copy that NextKast polled
# as a remote URL stream). Delivered here via lib/delivery.py straight
# into the existing WxForecast/forecast.mp3 and WxObs/current_obs.mp3
# categories instead -- see current_temp.py's header comment for why
# the old urgent_mix-append trick is no longer needed. All forecast
# fetch/cache/precip-merge/pronunciation logic below is unchanged --
# none of it was NextKast-specific.
#
# Two modes are supported via --mode:
#   3day -> 6 forecast periods, "next three days" framing
#   1day -> 2 forecast periods, "looking ahead" framing
#
# Voice selection via --voice is persona-agnostic:
#   auto             -> consult WeatherConfig.voice_schedule now
#   any other string -> resolve that WeatherVoicePersona slot explicitly
# Current checked-in systemd service templates use --voice auto. Their
# historical day/night filenames describe cadence only and have no
# persona semantics. See deploy/wx-forecast-*.service and paired timers.
#
# The forecast cache is shared across both modes: NWS is hit once per
# fetch, the full periods list is stored, and each mode slices at speak
# time. So a successful 1day fetch also satisfies a 3day fetch shortly
# after if NWS is briefly unreachable for the second call.
#
# Author: Justin Reed

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import requests
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from delivery import deliver
from notify import notify
import voices
from voices import VoiceResolutionError, resolve_voice
from wxconfig import load_weather_config, resolve_weather_data_dir

# ---------------- CONFIGURATION ----------------

BASE_DIR = Path(__file__).resolve().parent
CFG = load_weather_config()
DATA_DIR = resolve_weather_data_dir(CFG)

DATA_FILE = DATA_DIR / "processed_weather.json"
SMOOTHED_WIND_FILE = DATA_DIR / "smoothed_wind.json"
SKY_FILE = DATA_DIR / "sky_condition.json"
WARN_FILE = DATA_DIR / "warn.txt"
# Detailed watches/warnings JSON (description-only variant per entry) --
# used by the alert-append block below. wx_alert.py's fingerprint-driven
# urgent insert still consumes the "text" field of the same file with
# safety instructions included; the forecast uses "text_core" so
# listeners get the who/what/where/until-when clauses on every airing
# without the "Heat stroke is an emergency, call 9 1 1" tail repeating.
WATCHWARN_FILE = DATA_DIR / "active_watches_warnings.json"
# Detailed AMBER/BLU/MEP JSON written by amber_poll.py. Same shape as
# WATCHWARN_FILE: each entry has "text" (urgent-insert version) and
# "text_core" (forecast-append version). AMBER content typically
# keeps the phone-number tail in both variants -- that's the whole
# point of the alert, unlike weather safety instructions.
AMBER_FILE = DATA_DIR / "active_amber_alerts.json"

# Shared NWS forecast cache. Both modes write the FULL periods list (as
# JSON) to this file on a successful fetch, and read from it on fetch
# failure. The 1day mode slices to its 2 periods at speak time.
FORECAST_CACHE_FILE = DATA_DIR / "wx_forecast_cache.json"

FORECAST_URL = (
    f"https://api.weather.gov/gridpoints/{CFG['nws_forecast_office']}/"
    f"{CFG['nws_forecast_grid_x']},{CFG['nws_forecast_grid_y']}/forecast"
)

NWS_HEADERS = {
    "User-Agent": "OakGroveRadio WX Bot (oakgroveradio@gmail.com)",
    "Accept": "application/geo+json"
}
NWS_TIMEOUT = 15

ID3_ARTIST = "Oak Grove Radio"

# ---------------- MODES ----------------

MODES = {
    "3day": {
        "periods": 6,
        "id3_prefix": "3-Day Forecast",
        "intro": "Looking ahead over the next three days:",
        "category_code": "WxForecast",
        "dest_filename": "forecast.mp3",
    },
    "1day": {
        "periods": 2,
        "id3_prefix": "Current Observation",
        "intro": "Looking ahead",
        "category_code": "WxObs",
        "dest_filename": "current_obs.mp3",
    },
}

# ---------------- PRONUNCIATION REPLACEMENTS ----------------

PRONUNCIATION_MAP = {
    r"\bwind 0 to\b": "wend up to",
    r"\bwind\b": "wend",
    r"\bMinneapolis\b": "Minniapolis",
    r"\bsouthwest\b": "South-west",
    # Kokoro's phonemizer treats the "." in a decimal number like
    # "20.2 feet" as a sentence-end pause and silently drops the
    # "point" entirely, so the listener hears "twenty [pause] two feet"
    # instead of "twenty point two feet". Spelling it out prevents the
    # misparse. Anchored with a lookbehind/lookahead so it only fires
    # between two digit runs -- won't touch times like "4:36 PM" (uses
    # a colon anyway) or end-of-sentence periods.
    r"(?<=\d)\.(?=\d)": " point ",
}

# ---------------- UTILITY ----------------

def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def wind_direction_to_compass(deg):
    try:
        deg = float(deg)
    except Exception:
        return "unknown"
    dirs = ["North","Northeast","East","Southeast","South","Southwest","West","Northwest"]
    return dirs[int((deg + 22.5) / 45) % 8]

def format_rainfall_inches(a):
    h = round(a * 100)
    if a >= 1:
        whole = int(a)
        rem = h - (whole * 100)
        unit = "inch" if whole == 1 else "inches"
        if rem > 0:
            return f"{whole} and {rem} hundredths of an {unit}"
        return f"{whole} {unit}"
    return f"{h} hundredths of an inch"

# ---------------- FORECAST ----------------

def get_periods():
    """Fetch the full NWS periods list (typically 14 periods) and cache
    it. On fetch failure, fall back to the cached periods list with a
    staleness warning if old."""
    try:
        r = requests.get(FORECAST_URL, headers=NWS_HEADERS, timeout=NWS_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        periods = data.get("properties", {}).get("periods", []) or []
        if periods:
            try:
                with open(FORECAST_CACHE_FILE, "w") as f:
                    json.dump(periods, f)
                log(f"Forecast periods cached to {FORECAST_CACHE_FILE} ({len(periods)} periods)")
            except Exception as e:
                log(f"Could not write forecast cache: {e}")
        return periods
    except Exception as e:
        log(f"Forecast fetch failed: {e}")
        if os.path.exists(FORECAST_CACHE_FILE):
            try:
                age_sec = time.time() - os.path.getmtime(FORECAST_CACHE_FILE)
                age_hr = age_sec / 3600.0
                if age_hr > 6:
                    log(f"WARNING: cached forecast is {age_hr:.1f} hours old - "
                        f"NWS has been unreachable for an extended period.")
                with open(FORECAST_CACHE_FILE, "r") as f:
                    cached = json.load(f)
                if isinstance(cached, list) and cached:
                    log(f"Using cached forecast from {FORECAST_CACHE_FILE} (age {age_hr:.1f}h)")
                    return cached
            except Exception as e2:
                log(f"Failed to read cached forecast: {e2}")
        return []

def speak_forecast(periods):
    """Render a list of NWS period dicts into a single broadcast-ready
       string. Applies the precip merger and mph normalization."""
    lines = []
    mph_replaced = False
    for p in periods:
        name = p.get("name", "")
        forecast = p.get("detailedForecast", "")
        if not forecast:
            continue
        try:
            forecast = merge_precip_clauses(name, forecast)
        except Exception as e:
            log(f"Precip merger error on '{name}', using original: {e}")
        def replace_mph(m):
            nonlocal mph_replaced
            if not mph_replaced:
                mph_replaced = True
                return " miles per hour"
            return ""
        forecast = re.sub(r"\s*mph\b", replace_mph, forecast)
        forecast = re.sub(r"\s{2,}", " ", forecast).strip()
        lines.append(f"{name}: {forecast}")
    return " ".join(lines)


# ---------------- Precipitation-clause merger ----------------
#
# Collapses runs of adjacent precipitation clauses in NWS forecast prose
# into a single readable summary. See PROJECT_NOTES.md / the original
# script for the full strategy writeup -- unchanged from the source.

_PRECIP_PATTERNS = [
    (r"\bshowers?\s+and\s+thunderstorms?\b", "rain", "showers and thunderstorms", 3),
    (r"\bthunderstorms?\b",                  "rain", "showers and thunderstorms", 3),
    (r"\brain\s+showers?\b",                 "rain", "rain showers",              2),
    (r"\bshowers?\b",                        "rain", "showers",                   1),
    (r"\brain\b",                            "rain", "rain",                      1),
    (r"\bsnow\s+showers?\b",                 "snow", "snow showers",              2),
    (r"\bsnow\b",                            "snow", "snow",                      1),
]

_LIKELIHOOD_PREFIXES = [
    ("a slight chance of",  1),
    ("slight chance of",    1),
    ("a chance of",         2),
    ("chance of",           2),
]

_TIME_RE = re.compile(r"(\d{1,2})\s*(am|pm)", re.IGNORECASE)

def _parse_hour(token):
    m = _TIME_RE.fullmatch(token.strip())
    if not m: return None
    h = int(m.group(1))
    ap = m.group(2).lower()
    if ap == "am": return 0 if h == 12 else h
    return 12 if h == 12 else h + 12

def _extract_window(clause):
    c = clause.lower()
    m = re.search(r"between\s+(\d{1,2}\s*[ap]m)\s+and\s+(\d{1,2}\s*[ap]m)", c)
    if m:
        s, e = _parse_hour(m.group(1)), _parse_hour(m.group(2))
        if s is None or e is None: return None
        return (s, e, "between")
    m = re.search(r"\bbefore\s+(\d{1,2}\s*[ap]m)", c)
    if m:
        e = _parse_hour(m.group(1))
        if e is None: return None
        return (None, e, "before")
    m = re.search(r"\bafter\s+(\d{1,2}\s*[ap]m)", c)
    if m:
        s = _parse_hour(m.group(1))
        if s is None: return None
        return (s, None, "after")
    if re.search(r"\b(this|tonight|tomorrow|morning|afternoon|evening|overnight)\b", c):
        return None
    return (None, None, "bare")

def _detect_precip(clause):
    best = None
    for pat, fam, label, rank in _PRECIP_PATTERNS:
        if re.search(pat, clause, re.IGNORECASE):
            if best is None or rank > best[2]:
                best = (fam, label, rank)
    return best

def _detect_likelihood_rank(clause):
    cl = clause.lower().strip().lstrip(", ")
    if re.search(r"\blikely\b", cl): return 4
    for phrase, rank in _LIKELIHOOD_PREFIXES:
        if cl.startswith(phrase): return rank
    return 3

def _windows_contiguous(a, b):
    if a is None or b is None: return False
    a_s, a_e, a_k = a
    b_s, b_e, b_k = b
    if a_k == "before"  and b_k == "between": return a_e == b_s
    if a_k == "between" and b_k == "between": return a_e == b_s
    if a_k == "between" and b_k == "after":   return a_e == b_s
    if a_k == "before"  and b_k == "after":   return a_e == b_s
    return False

def _merge_two_windows(a, b):
    a_s, _, _ = a
    _, b_e, _ = b
    new_s, new_e = a_s, b_e
    if new_s is None and new_e is None: return (None, None, "bare")
    if new_s is None: return (None, new_e, "before")
    if new_e is None: return (new_s, None, "after")
    return (new_s, new_e, "between")

def _format_hour(h):
    if h == 0: return "12am"
    if h == 12: return "12pm"
    if h < 12: return f"{h}am"
    return f"{h - 12}pm"

def _is_day_period(name):
    n = (name or "").lower()
    return ("night" not in n) and ("overnight" not in n) and ("tonight" not in n)

def _is_night_period(name):
    n = (name or "").lower()
    return ("night" in n) or ("overnight" in n) or ("tonight" in n)

_NATURAL_PHRASES = [
    ("this morning",   _is_day_period,    7, 11),
    ("this afternoon", _is_day_period,   13, 17),
    ("this evening",   _is_day_period,   19, 21),
]

def _try_natural_phrase(window, period_name):
    s, e, k = window
    if k != "between": return None
    if s is None or e is None: return None
    if (e - s) < 4: return None
    for phrase, filt, inner_s, inner_e in _NATURAL_PHRASES:
        if not filt(period_name): continue
        if s <= inner_s and e >= inner_e:
            return phrase
    return None

def _render_clause(precip_label, likelihood_rank, window, period_name):
    s, e, k = window
    if likelihood_rank == 1:   prefix, suffix = "a slight chance of ", ""
    elif likelihood_rank == 2: prefix, suffix = "a chance of ", ""
    elif likelihood_rank == 4: prefix, suffix = "", " likely"
    else:                      prefix, suffix = "", ""

    nat = _try_natural_phrase(window, period_name)
    if nat is not None:
        tail = " " + nat
    elif k == "between":
        tail = f" between {_format_hour(s)} and {_format_hour(e)}"
    elif k == "before":
        tail = f" before {_format_hour(e)}"
    elif k == "after":
        tail = f" after {_format_hour(s)}"
    else:
        tail = ""

    return f"{prefix}{precip_label}{suffix}{tail}"

def merge_precip_clauses(period_name, forecast_text):
    sentences = re.split(r"(?<=\.)\s+(?=[A-Z])", forecast_text.strip())
    if not sentences: return forecast_text

    first = sentences[0]
    rest = sentences[1:]

    if _detect_precip(first) is None:
        return forecast_text

    has_period = first.endswith(".")
    body = first[:-1] if has_period else first

    clause_texts = re.split(r",\s*then\s+", body)
    if len(clause_texts) < 2:
        return forecast_text

    clauses = []
    for t in clause_texts:
        text = t.strip()
        clauses.append({
            "text":       text,
            "window":     _extract_window(text),
            "precip":     _detect_precip(text),
            "likelihood": _detect_likelihood_rank(text),
        })

    out = []
    i = 0
    while i < len(clauses):
        cur = clauses[i]
        mergeable = (cur["window"] is not None and cur["precip"] is not None)
        if not mergeable:
            out.append(cur["text"])
            i += 1
            continue

        run_precip     = cur["precip"]
        run_likelihood = cur["likelihood"]
        run_window     = cur["window"]
        j = i + 1

        while j < len(clauses):
            nxt = clauses[j]
            if nxt["window"] is None or nxt["precip"] is None: break
            if run_precip[1] != nxt["precip"][1]: break
            if run_likelihood != nxt["likelihood"]: break

            if nxt["window"][2] == "bare":
                if j != len(clauses) - 1: break
                if run_window[0] is None: break
                run_window = (run_window[0], None, "after")
                j += 1
                break

            if not _windows_contiguous(run_window, nxt["window"]): break
            run_window = _merge_two_windows(run_window, nxt["window"])
            j += 1

        if j > i + 1:
            merged = _render_clause(run_precip[1], run_likelihood, run_window, period_name)
            if not out:
                merged = merged[0].upper() + merged[1:]
            out.append(merged)
            i = j
        else:
            out.append(cur["text"])
            i += 1

    new_first = ", then ".join(out)
    if has_period: new_first += "."
    return " ".join([new_first] + rest)

# ---------------- ANNOUNCEMENT BUILDER ----------------

def build_announcement(mode, voice):
    data = load_json(DATA_FILE)
    smoothed = load_json(SMOOTHED_WIND_FILE)
    sky = load_json(SKY_FILE)

    temp_f = round(float(data.get("tempf", 0)))
    feels_like = round(float(data.get("feels_like", temp_f)))
    humidity = round(float(data.get("humidity", 0)))
    pressure = round(float(data.get("baromrelin", 0)), 2)
    rainfall = float(data.get("dailyrainin", 0))
    hourly = float(data.get("hourlyrainin", 0))

    wind_mph = round(smoothed.get("speed", data.get("windspeedmph", 0)))
    wind_gst = round(smoothed.get("gust", data.get("windgustmph", 0)))
    wind_dir = smoothed.get("dir", data.get("winddir", 0))
    wind_compass = wind_direction_to_compass(wind_dir)

    sky_condition = sky.get("condition", "clear").capitalize()
    trend = sky.get("pressure_trend", "steady")

    if feels_like != temp_f:
        obs = f"Currently in Minneapolis it's {sky_condition} and {temp_f} degrees but it feels like {feels_like},"
    else:
        obs = f"Currently in Minneapolis it's {sky_condition} and {temp_f} degrees,"

    if wind_gst < 1:
        wind_str = "The wind is calm"
    elif wind_gst < 3:
        wind_str = "The wind is light and variable"
    elif wind_gst == wind_mph:
        wind_str = f"The wind is from the {wind_compass} at {wind_mph}"
    else:
        wind_str = f"The wind is from the {wind_compass} at {wind_mph} gusting to {wind_gst}"

    announcement = (
        f"{obs} with the humidity at {humidity} percent. {wind_str}, "
        f"and the barometer is {pressure} inches and {trend}."
    )

    if rainfall >= 0.01:
        rain_str = f" We've had {format_rainfall_inches(rainfall)} of rainfall since midnight."
        if hourly >= 0.01:
            rain_str += f" Of that, {format_rainfall_inches(hourly)} fell in the past hour."
        announcement += rain_str

    periods = get_periods()
    sliced = periods[: mode["periods"]]
    forecast = speak_forecast(sliced)
    if forecast:
        announcement += f" {mode['intro']} {forecast}"

    alert_block = _build_alert_block()
    if alert_block:
        announcement += f" {alert_block}"

    amber_block = _build_amber_block()
    if amber_block:
        announcement += f" {amber_block}"

    # Signoff goes LAST so the whole piece -- forecast + optional
    # NWS alert detail + optional AMBER content -- lands under a single
    # announcer bow-out instead of the signoff sitting mid-message.
    announcement += f" For Oak Grove Radio ninety-eight point five, {voice['signoff']}"
    return announcement


# Rotating connectors used to introduce each alert AFTER the first.
# Written as sentence-lead-ins (capital letter, trailing comma) so that
# after the previous alert's terminal period the flow reads:
#   "...heat stroke. And, A Flood Warning is in effect."
# The first alert doesn't get one -- the transition phrase already
# introduces it. If there are ever 4+ alerts, the sequence rotates.
_ALERT_CONNECTORS = ("And,", "Also,", "In addition,")


def _build_alert_block():
    """Read active watches/warnings and format the description-only
    variant of each as a single string ready to append to the
    forecast body. Returns "" (no block, no transition phrase) if
    there are no active alerts."""
    if not os.path.exists(WATCHWARN_FILE):
        return ""
    try:
        with open(WATCHWARN_FILE) as f:
            entries = json.load(f)
    except Exception as e:
        log(f"Could not read {WATCHWARN_FILE}: {e}")
        return ""

    cores = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        core = (entry.get("text_core") or "").strip()
        if core:
            cores.append(core)

    if not cores:
        return ""

    # Full sentence ("the following alerts.") with a real terminal
    # period so Kokoro treats it as a genuine sentence break and
    # gives the listener a real beat before the first alert. A
    # trailing colon reads as a mid-sentence pause and rushes into
    # the first alert.
    pieces = ["The National Weather Service has issued the following alerts."]
    for i, core in enumerate(cores):
        if i == 0:
            pieces.append(core)
        else:
            connector = _ALERT_CONNECTORS[(i - 1) % len(_ALERT_CONNECTORS)]
            pieces.append(f"{connector} {core}")
    return " ".join(pieces)


def _build_amber_block():
    """Read active AMBER/BLU/MEP alerts and format them as an append-
    ready block. Runs after the NWS block so listeners hear the more
    common weather content first and the (rarer, higher-attention)
    AMBER content just before the signoff. Returns "" (no block) when
    there are no active alerts -- most days, this is the case."""
    if not os.path.exists(AMBER_FILE):
        return ""
    try:
        with open(AMBER_FILE) as f:
            entries = json.load(f)
    except Exception as e:
        log(f"Could not read {AMBER_FILE}: {e}")
        return ""

    cores = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        core = (entry.get("text_core") or "").strip()
        if core:
            cores.append(core)

    if not cores:
        return ""

    # Single vs plural lead-in reads naturally either way; AMBER is
    # the alert most listeners recognize by name, so we use that
    # word rather than "the following alerts" (which listeners might
    # confuse with the NWS block above).
    if len(cores) == 1:
        pieces = ["The following AMBER-family alert is currently active."]
    else:
        pieces = ["The following AMBER-family alerts are currently active."]
    for i, core in enumerate(cores):
        if i == 0:
            pieces.append(core)
        else:
            connector = _ALERT_CONNECTORS[(i - 1) % len(_ALERT_CONNECTORS)]
            pieces.append(f"{connector} {core}")
    return " ".join(pieces)

# ---------------- AUDIO GENERATION ----------------

def generate_wav_with_piper(text, wav_path, voice):
    """Preserves the original name/signature for the caller below;
    synthesis itself is voices.synthesize()'s job, via the canonical
    shared TTS CLI -- see lib/voices.py."""
    for pattern, repl in PRONUNCIATION_MAP.items():
        text = re.sub(pattern, repl, text, flags=re.IGNORECASE)

    ok = voices.synthesize(text, wav_path, voice)
    if ok:
        log(f"WAV file generated: {wav_path}")
    return ok

def convert_to_mp3(wav_file, mp3_file, id3_title):
    if not os.path.exists(wav_file):
        log(f"ERROR: Missing WAV file: {wav_file}")
        return False
    try:
        os.remove(mp3_file)
    except FileNotFoundError:
        pass
    except OSError as e:
        log(f"Could not clear stale MP3 at {mp3_file}: {e}")
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
            check=True
        )
        log(f"Converted WAV to MP3 (44.1 kHz, stereo, 320 kbps): {mp3_file}")
        return True
    except subprocess.CalledProcessError as e:
        log(f"FFmpeg conversion failed: {e}")
        return False

# ---------------- MAIN ----------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate the Oak Grove Radio weather announcement.",
    )
    parser.add_argument(
        "--mode",
        choices=sorted(MODES.keys()),
        required=True,
        help="Forecast length: 3day (6 periods) or 1day (2 periods).",
    )
    parser.add_argument(
        "--voice",
        required=True,
        help="Persona slot key, or auto to use WeatherConfig.voice_schedule for the current hour.",
    )
    return parser.parse_args()


def main():
    """Returns True on success, False on any real synthesis-generation
    failure (voice resolution, canonical CLI, MP3 conversion, or
    delivery)."""
    args = parse_args()
    mode = MODES[args.mode]
    try:
        voice_key, voice = resolve_voice(CFG, args.voice)
    except VoiceResolutionError as e:
        log(f"Voice resolution failed: {e}")
        notify(f"wx_forecast ({args.mode}) FAILED", f"Voice resolution failed: {e}")
        return False
    if args.voice == "auto":
        log(f"Voice auto-selected: {voice_key}")
    id3_title = f"{mode['id3_prefix']} ({voice['name']})"

    tmp_dir = f"/tmp/syndicated-wx-forecast-{args.mode}"
    output_wav = os.path.join(tmp_dir, f"{args.mode}.wav")
    output_mp3 = os.path.join(tmp_dir, f"{args.mode}.mp3")

    try:
        log(f"=== WX Forecast Announcer Started (mode={args.mode} voice={voice_key}) ===")
        announcement = build_announcement(mode, voice)
        log(f"Announcement: {announcement}")
        os.makedirs(tmp_dir, exist_ok=True)

        synth_result = generate_wav_with_piper(announcement, output_wav, voice)
        if not synth_result:
            notify(
                f"wx_forecast ({args.mode}) FAILED",
                f"Synthesis failed ({synth_result.reason}).\nAnnouncement: {announcement}",
            )
            return False

        if not convert_to_mp3(output_wav, output_mp3, id3_title):
            notify(f"wx_forecast ({args.mode}) FAILED", "MP3 conversion failed.")
            return False

        try:
            dest = deliver(output_mp3, mode["category_code"], mode["dest_filename"])
            log(f"Delivered and synced: {dest}")
        except Exception as e:
            log(f"Delivery failed: {e}")
            notify(f"wx_forecast ({args.mode}) FAILED", f"Delivery failed: {e}")
            return False

        log("=== Completed successfully ===")
        return True

    finally:
        # tmp_dir is exclusive to this run (mode-specific) -- /tmp is
        # tmpfs (RAM-backed) on this box.
        shutil.rmtree(tmp_dir, ignore_errors=True)

if __name__ == "__main__":
    sys.exit(0 if main() else 1)
