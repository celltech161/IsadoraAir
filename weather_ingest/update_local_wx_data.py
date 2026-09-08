#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# GW3000 Master Weather Data Updater
# ------------------------------------------------
# Reads latest_weather.json (written by IsadoraAir's weather Django app,
# ported from /home/jreed/wx_scripts/app.py), updates pressure
# history/trend, computes feels_like and dew_point, updates RDS files
# and alerts, and determines sky condition using NWS recent
# observations.
#
# Ported from /home/jreed/auto_dl_scripts's sibling
# /home/jreed/wx_scripts/update_local_wx_data.py (originally on the
# kogr-sc box). The meteorology/alert-text logic below is unchanged --
# it was never NextKast-specific. What changed:
#   - All paths use IsadoraAir's admin-editable WEATHER_DATA_DIR via
#     lib/wxconfig.py instead of /root/gw3000_receiver + /root/wxdata.
#   - Station location / NWS zone / grid / cloud stations come from
#     WeatherConfig (admin-editable) via lib/wxconfig.py instead of
#     hardcoded constants.
#   - The severe-alert trigger no longer runs wx_alert_statement.py
#     (which built an ogremote urgent_pa mix -- not ported, doesn't
#     exist on this box). It now runs wx_alert.py, which delivers a
#     fresh Piper clip straight into the WxAlert category and fires
#     engine.py's insert_urgent command so it plays at the very next
#     track boundary, live-verified working.
#
# Writes:
#   - DATA_DIR/processed_weather.json  (via update_rds)
#   - DATA_DIR/sky_condition.json
#   - DATA_DIR/rds_temp.txt
#   - DATA_DIR/rds_wind.txt
#
# Author: Justin Reed

import json
import hashlib
import logging
import os
import subprocess
import sys
import math
import requests
import re
import fcntl
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from wxconfig import load_weather_config, resolve_weather_data_dir

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [wx_updater] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------- CONFIG ----------------
CFG = load_weather_config()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = resolve_weather_data_dir(CFG)

DATA_FILE = DATA_DIR / "latest_weather.json"
PROCESSED_FILE = DATA_DIR / "processed_weather.json"
PRESSURE_HISTORY_FILE = DATA_DIR / "pressure_history.json"
SKY_FILE = DATA_DIR / "sky_condition.json"
SMOOTHED_WIND_FILE = DATA_DIR / "smoothed_wind.json"
NWS_CACHE_FILE = DATA_DIR / "nws_obs_cache.json"

# RDS outputs -- consumed by an RBDSMessage row (source_type="file")
# once wired up in admin; see PROJECT_NOTES.md.
RDS_FILE_1 = DATA_DIR / "rds_temp.txt"
RDS_FILE_2 = DATA_DIR / "rds_wind.txt"

# Alerts
ALERT_ZONE = CFG["nws_alert_zone"]
ALERT_URL = f"https://api.weather.gov/alerts/active?zone={ALERT_ZONE}"
ALERT_STATUS = DATA_DIR / "alert.txt"
ALERT_DESCRIPTION = DATA_DIR / "warn.txt"

# Alert change-detection. We fingerprint the cleaned alert text on each
# run; if it differs from the prior fingerprint, fire the current_temp
# announcer immediately so listeners hear the change at the next song
# boundary instead of waiting for the next regular hit. Triggers on ANY
# change: new alerts, escalations, de-escalations, expirations, full
# clears.
ALERT_FINGERPRINT_FILE = DATA_DIR / "alert_fingerprint.txt"
CURRENT_TEMP_SCRIPT = str(BASE_DIR / "current_temp.py")

# Structured Watch/Warning data for wx_alert.py, which turns it into
# Piper TTS audio and inserts it directly into the live on-air queue.
# Written every cycle by update_wx_alerts(); wx_alert.py is only
# actually triggered (see _trigger_wx_alert) when the alert fingerprint
# changes, same as the current_temp.py trigger below.
WX_WATCHWARN_FILE = DATA_DIR / "active_watches_warnings.json"
WX_ALERT_SCRIPT = str(BASE_DIR / "wx_alert.py")

# Location
LAT = CFG["station_lat"]
LON = CFG["station_lon"]

SUN_ALT_THRESHOLD = CFG["sun_alt_threshold_deg"]

# Prefer NWS/METAR if the newest station report is within this many minutes
NWS_RECENT_MIN = 60

NWS_STATIONS = {
    code: f"https://api.weather.gov/stations/{code}/observations/latest"
    for code in CFG["nws_cloud_stations"]
}

ALERT_KEYWORDS = [
    "Tornado Warning", "Severe Thunderstorm Warning",
    "Tornado Watch", "Severe Thunderstorm Watch"
]

VERBOSE = True

# Prevents overlapping cron runs of this script. Matters more now than it
# used to: update_wx_alerts() can block for up to 120s waiting on
# wx_alert.py (see _trigger_wx_alert), so a slow cycle could still be
# running when the next 5-minute cron tick fires. Without this, two
# overlapping instances could race on ALERT_FINGERPRINT_FILE (both read
# the same prior value before either writes) and both decide to trigger
# wx_alert.py/current_temp.py at once.
LOCKFILE = "/tmp/syndicated-wx-updater.lock"

def acquire_lock(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    f = open(path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(str(os.getpid()))
        f.flush()
        return f
    except BlockingIOError:
        f.close()
        raise RuntimeError("Another instance is already running.")

session = requests.Session()
session.headers.update({"User-Agent": "OakGroveRadio-WX/1.0"})

# ---------------- FILE UTILS ----------------

def safe_write_json(path, obj):
    # Atomic write: stage to a unique tmp filename, fsync, then os.replace
    # onto the destination -- the announcer scripts read
    # processed_weather.json / warn.txt while building broadcast text, and
    # this eliminates the window where they could see a half-written file.
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(obj, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception as e:
        log.error("Error writing %s: %s", path, e)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

def safe_write_text(path, text):
    tmp_path = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp_path, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception as e:
        log.error("Error writing %s: %s", path, e)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

# ---------------- SOLAR GEOMETRY ----------------

def sun_above_horizon(lat, lon, dt):
    doy = dt.timetuple().tm_yday
    timezone_offset_hours = lon / 15.0
    solar_time = dt + timedelta(hours=timezone_offset_hours)
    B = math.radians((360 / 365) * (doy - 81))
    eq_time = 9.87 * math.sin(2 * B) - 7.53 * math.cos(B) - 1.5 * math.sin(B)
    true_solar_minutes = (solar_time.hour * 60 + solar_time.minute + solar_time.second / 60 + eq_time)
    true_solar_time = (true_solar_minutes / 60.0) % 24
    hour_angle = (true_solar_time - 12.0) * 15.0
    decl = 23.44 * math.sin(math.radians(360 * (284 + doy) / 365))
    solar_alt = math.degrees(math.asin(
        math.sin(math.radians(lat)) * math.sin(math.radians(decl)) +
        math.cos(math.radians(lat)) * math.cos(math.radians(decl)) * math.cos(math.radians(hour_angle))
    ))
    if VERBOSE and sys.stdout.isatty():
        log.debug("[Solar] altitude=%.2f deg, threshold=%.1f deg", solar_alt, SUN_ALT_THRESHOLD)
    return solar_alt > SUN_ALT_THRESHOLD

# ---------------- PRESSURE HISTORY & TREND ----------------

def _load_pressure_history():
    if not os.path.exists(PRESSURE_HISTORY_FILE):
        return []
    try:
        with open(PRESSURE_HISTORY_FILE, "r") as f:
            raw = json.load(f)
    except Exception:
        return []
    if not isinstance(raw, list):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    out = []
    for h in raw:
        try:
            t = datetime.fromisoformat(h["time"]).replace(tzinfo=timezone.utc)
            p = float(h["pressure"])
        except Exception:
            continue
        if t >= cutoff:
            out.append((t, p))
    return out

def update_pressure_history(current_pressure):
    now = datetime.now(timezone.utc)
    parsed = _load_pressure_history()
    history = [{"time": t.isoformat(), "pressure": p} for t, p in parsed]
    history.append({"time": now.isoformat(), "pressure": float(current_pressure)})
    safe_write_json(PRESSURE_HISTORY_FILE, history)
    if VERBOSE:
        log.info("Updated pressure history at %s", now.isoformat())

def get_pressure_trend(current_pressure):
    now = datetime.now(timezone.utc)
    parsed = _load_pressure_history()
    threshold = now - timedelta(hours=3)
    past = [(t, p) for (t, p) in parsed if t <= threshold]
    if not past:
        return "steady"
    delta = float(current_pressure) - past[0][1]
    if delta >= 0.09: return "rising rapidly"
    if delta >= 0.03: return "rising"
    if delta <= -0.09: return "falling rapidly"
    if delta <= -0.03: return "falling"
    return "steady"

# ---------------- THERMO & WIND ----------------

def calculate_dew_point(temp_f, humidity):
    try:
        t_c = (float(temp_f) - 32) * 5 / 9
        a, b = 17.625, 243.04
        alpha = ((a * t_c) / (b + t_c)) + math.log(float(humidity) / 100)
        dp_c = (b * alpha) / (a - alpha)
        return dp_c * 9 / 5 + 32
    except Exception:
        return None

def calculate_feels_like(temp_f, hum, wind):
    """Return NWS-spec apparent temperature: heat index above 80F,
    wind chill at/below 50F with wind >= 3 mph, otherwise the raw air
    temp. When HI comes out within rounding of the actual temp the
    caller's `round(feels_like) != round(temp_f)` gate cleanly
    suppresses the "feels like" phrase -- no need to gate here on
    humidity, and doing so silences valid HI at hot + mid-humidity
    conditions (e.g. 100F/37% RH would compute to ~107F but was
    previously suppressed by the arbitrary hum >= 40 threshold)."""
    try:
        temp_f = float(temp_f)
        hum = float(hum)
        wind = float(wind)
    except Exception:
        return round(temp_f, 1)
    if temp_f <= 50 and wind >= 3:
        wc = 35.74 + 0.6215 * temp_f - 35.75 * (wind ** 0.16) + 0.4275 * temp_f * (wind ** 0.16)
        return round(wc, 1)
    if temp_f >= 80:
        # NWS Rothfusz regression -- the base formula.
        hi = (-42.379 + 2.04901523 * temp_f + 10.14333127 * hum - 0.22475541 * temp_f * hum
              - 6.83783e-3 * temp_f ** 2 - 5.481717e-2 * hum ** 2 + 1.22874e-3 * temp_f ** 2 * hum
              + 8.5282e-4 * temp_f * hum ** 2 - 1.99e-6 * temp_f ** 2 * hum ** 2)
        # Low-humidity correction (dry heat overestimates otherwise).
        if hum < 13 and 80 <= temp_f <= 112:
            hi -= ((13 - hum) / 4.0) * math.sqrt((17 - abs(temp_f - 95)) / 17.0)
        # High-humidity correction (muggy at mid-80s underestimates).
        elif hum > 85 and 80 <= temp_f <= 87:
            hi += ((hum - 85) / 10.0) * ((87 - temp_f) / 5.0)
        return round(hi, 1)
    return round(temp_f, 1)

def wind_direction_to_compass(deg):
    try:
        deg = float(deg)
    except Exception:
        return "unknown"
    dirs = ['North', 'Northeast', 'East', 'Southeast', 'South', 'Southwest', 'West', 'Northwest']
    return dirs[int((deg + 22.5) / 45) % 8]

# ---------------- SKY CONDITION ----------------

def describe_sky_condition(blended_cover, is_daytime=False):
    if blended_cover is None:
        return "Unknown"
    if blended_cover < 0.12:
        return "Sunny" if is_daytime else "Clear"
    elif blended_cover < 0.37:
        return "Mostly sunny" if is_daytime else "Mostly clear"
    elif blended_cover < 0.62:
        return "Partly cloudy"
    elif blended_cover < 0.87:
        return "Mostly cloudy"
    else:
        return "Cloudy"

def refine_sky_with_precip(base_condition, wx_desc, rain_rate):
    desc = base_condition
    wx_desc = (wx_desc or "").lower()
    rain_rate = float(rain_rate or 0.0)
    if "thunder" in wx_desc or "storm" in wx_desc:
        return f"{base_condition} with storms in the area"
    if any(word in wx_desc for word in ["snow", "sleet", "freezing rain", "freezing drizzle"]):
        if "light" in wx_desc:
            return f"{base_condition} with light snow"
        elif "heavy" in wx_desc:
            return f"{base_condition} with heavy snow"
        else:
            return f"{base_condition} with snow"
    if rain_rate > 0.05:
        if rain_rate > 0.3:
            return f"{base_condition} with steady rain"
        else:
            return f"{base_condition} with rain showers"
    elif any(word in wx_desc for word in ["rain", "showers"]):
        if "light" in wx_desc:
            return f"{base_condition} with light rain showers"
        elif "heavy" in wx_desc:
            return f"{base_condition} with heavy rain showers"
        else:
            return f"{base_condition} with rain showers"
    if any(word in wx_desc for word in ["fog", "mist", "haze", "drizzle"]):
        return f"{base_condition} with {wx_desc}"
    return desc

def get_nws_cloud_cover(force_refresh=False):
    now = datetime.now(timezone.utc)

    if not force_refresh and os.path.exists(NWS_CACHE_FILE):
        try:
            with open(NWS_CACHE_FILE, "r") as f:
                cache = json.load(f)
            ts = datetime.fromisoformat(cache.get("time"))
            age_min = max(0.0, (now - ts).total_seconds() / 60.0)
            if VERBOSE:
                log.info("NWS cache age: %.1f min (threshold 10.0 min)", age_min)
            if age_min < 10.0:
                if VERBOSE:
                    log.info("Using cached NWS observations.")
                results = cache.get("results", {})
                newest_age_min = compute_newest_station_age_min(results, now)
                return results, cache.get("average"), newest_age_min
        except Exception as e:
            log.warning("Cache read error, refetching NWS: %s", e)

    if VERBOSE:
        log.info("Refreshing NWS observations...")

    results = {}
    mapping = {
        "CLR": 0.0, "SKC": 0.0, "NSC": 0.0,
        "FEW": 0.15, "SCT": 0.40, "BKN": 0.70, "OVC": 1.00
    }

    newest_dt = None

    for code, url in NWS_STATIONS.items():
        try:
            r = session.get(url, timeout=8)
            if r.status_code == 200:
                props = r.json().get("properties", {})
                layers = props.get("cloudLayers", []) or []
                text_desc = props.get("textDescription", "Unknown")
                obs_time = props.get("timestamp")

                layer_amounts = [layer.get("amount") for layer in layers if layer.get("amount")]
                layer_fracs = [mapping[a] for a in layer_amounts if a in mapping]
                frac = max(layer_fracs) if layer_fracs else None

                obs_dt = None
                try:
                    if obs_time:
                        obs_dt = datetime.fromisoformat(obs_time.replace("Z", "+00:00"))
                        if newest_dt is None or obs_dt > newest_dt:
                            newest_dt = obs_dt
                except Exception:
                    obs_dt = None

                results[code] = {
                    "fraction": frac,
                    "nws_text": text_desc,
                    "amounts": layer_amounts,
                    "obs_time": obs_time
                }
            else:
                results[code] = {"fraction": None, "nws_text": f"HTTP {r.status_code}", "amounts": [], "obs_time": None}
        except Exception as e:
            log.error("Error fetching %s: %s", code, e)
            results[code] = {"fraction": None, "nws_text": "Error", "amounts": [], "obs_time": None}

    valid = [v["fraction"] for v in results.values() if v["fraction"] is not None]
    avg = sum(valid) / len(valid) if valid else None

    safe_write_json(NWS_CACHE_FILE, {"time": now.isoformat(), "results": results, "average": avg})

    newest_age_min = None
    if newest_dt:
        newest_age_min = max(0.0, (now - newest_dt).total_seconds() / 60.0)

    return results, avg, newest_age_min

def compute_newest_station_age_min(results, now_utc):
    newest_dt = None
    for info in (results or {}).values():
        obs_time = info.get("obs_time")
        try:
            if obs_time:
                dt = datetime.fromisoformat(obs_time.replace("Z", "+00:00"))
                if newest_dt is None or dt > newest_dt:
                    newest_dt = dt
        except Exception:
            pass
    if newest_dt is None:
        return None
    return max(0.0, (now_utc - newest_dt).total_seconds() / 60.0)

# ---------------- ALERTS ----------------

def normalize_times_for_tts(text):
    def _repl(m):
        hour = m.group(1)
        minute = m.group(2)
        ampm = m.group(3).upper()
        try:
            hour_int = int(hour)
            hour_str = str(hour_int)
        except Exception:
            hour_str = hour.lstrip("0") or hour
        if minute is None:
            return f"{hour_str} {ampm}"
        if minute == "00":
            return f"{hour_str} {ampm}"
        return f"{hour_str}:{minute} {ampm}"

    pattern = r"\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b"
    out = re.sub(pattern, _repl, text, flags=re.IGNORECASE)
    return " ".join(out.split())

def _localize_until_phrase(text):
    today = datetime.now().date()
    tomorrow = today + timedelta(days=1)

    months = "January|February|March|April|May|June|July|August|September|October|November|December"
    pat = re.compile(
        rf"until\s+(?P<month>{months})\s+(?P<day>\d{{1,2}})\s+at\s+(?P<time>\d{{1,2}}:\d{{2}}\s*(?:AM|PM))",
        re.IGNORECASE,
    )

    month_lookup = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    }

    def _replacer(m):
        month_name = m.group("month").lower()
        day_str = m.group("day")
        time_str = m.group("time")
        try:
            month_num = month_lookup[month_name]
            day_num = int(day_str)
            year = today.year
            try_date = datetime(year, month_num, day_num).date()
            if (today - try_date).days > 30:
                try_date = datetime(year + 1, month_num, day_num).date()
        except (ValueError, KeyError):
            return m.group(0)

        if try_date == today:
            return f"until {time_str}"
        if try_date == tomorrow:
            return f"until tomorrow at {time_str}"
        return m.group(0)

    return pat.sub(_replacer, text)


def clean_alert_text(text):
    text = text.replace("by NWS Topeka KS", "").replace("CDT", "").replace("CST", "")
    text = _localize_until_phrase(text)
    text = re.sub(r"\bissued\b.*?\buntil\b", r"is in effect until", text, flags=re.IGNORECASE)
    text = normalize_times_for_tts(text)
    text = " ".join(text.split())
    if not re.match(r"^(A|An|The)\b", text, re.IGNORECASE):
        text = "A " + text
    if not text.endswith("."):
        text += "."
    return text

def _alert_fingerprint(alert_strings):
    if not alert_strings:
        return "NONE"
    joined = "|".join(sorted(s.strip() for s in alert_strings if s.strip()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()

def _read_prior_fingerprint():
    if not os.path.exists(ALERT_FINGERPRINT_FILE):
        return None
    try:
        with open(ALERT_FINGERPRINT_FILE, "r") as f:
            return f.read().strip()
    except Exception as e:
        log.warning("Could not read prior alert fingerprint: %s", e)
        return None

# ---------------- WATCH/WARNING STATEMENT TEXT ----------------

def _is_watch_or_warning(event):
    event = (event or "").strip()
    return event.endswith("Watch") or event.endswith("Warning")

_LETTERS = re.compile(r"[A-Za-z]")

def _looks_all_caps(text):
    letters = _LETTERS.findall(text)
    if len(letters) < 10:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) > 0.8

def _sentence_case(text):
    if not _looks_all_caps(text):
        return text
    lowered = text.lower()
    pieces = re.split(r"([.!?]\s+)", lowered)
    out = []
    capitalize_next = True
    for piece in pieces:
        if capitalize_next and piece:
            piece = piece[0].upper() + piece[1:]
        out.append(piece)
        capitalize_next = bool(re.match(r"[.!?]\s+", piece))
    return "".join(out)

def _extract_nws_headline(props):
    params = props.get("parameters") or {}
    values = params.get("NWSheadline")
    if isinstance(values, list) and values and values[0]:
        return _sentence_case(values[0].strip())
    return None

def _extract_storm_location_sentence(description):
    if not description:
        return None
    m = re.search(
        r"\*\s*(At\s+\d{1,2}:?\d{2}\s*[AP]M.*?)(?=\n\s*\n|\n\s*HAZARD\.\.\.|\Z)",
        description, re.IGNORECASE | re.DOTALL
    )
    if not m:
        return None
    body = " ".join(m.group(1).split())
    return body if body else None

def _extract_hazard_source_impact(description):
    if not description:
        return None
    label_pattern = re.compile(r"\b(HAZARD|SOURCE|IMPACTS?)\.\.\.")
    matches = list(label_pattern.finditer(description))
    if not matches:
        return None
    raw = {}
    for i, m in enumerate(matches):
        label = "IMPACT" if m.group(1).startswith("IMPACT") else m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(description)
        body = description[start:end]
        bullet_cut = re.search(r"\n\s*\*\s", body)
        if bullet_cut:
            body = body[:bullet_cut.start()]
        body = " ".join(body.split())
        if body:
            raw.setdefault(label, body)
    if not raw:
        return None
    pieces = []
    if "HAZARD" in raw:
        pieces.append("The hazard is %s" % _lowercase_first_if_safe(raw["HAZARD"]))
    if "SOURCE" in raw:
        pieces.append("This is based on %s" % _lowercase_first_if_safe(raw["SOURCE"]))
    if "IMPACT" in raw:
        pieces.append(raw["IMPACT"])
    if not pieces:
        return None
    return ". ".join(p.rstrip(".") for p in pieces) + "."

def _extract_convective_warning(description, event):
    hsi = _extract_hazard_source_impact(description)
    if not hsi:
        return None
    pieces = []
    if event:
        pieces.append("%s %s is in effect" % (_indefinite_article(event), event))
    loc = _extract_storm_location_sentence(description)
    if loc:
        pieces.append(loc)
    pieces.append(hsi.rstrip("."))
    return ". ".join(p.rstrip(".") for p in pieces) + "."


def _lowercase_first_if_safe(text):
    words = text.split(" ", 2)
    if len(words) < 2 or not words[0]:
        return text
    if words[1][:1].isupper():
        return text
    return words[0][0].lower() + words[0][1:] + text[len(words[0]):]
_VOWEL_SOUND_START = re.compile(r"^[AEIOU]", re.IGNORECASE)

def _indefinite_article(word):
    return "An" if _VOWEL_SOUND_START.match(word) else "A"

def _extract_bullets(description, event=""):
    if not description:
        return None
    blocks = re.split(r"\n\s*\n", description.strip())
    wanted = ("WHAT", "WHERE", "WHEN", "IMPACTS")
    raw = {}
    for block in blocks:
        block = block.strip()
        m = re.match(r"^\*\s*([A-Z]+)\.\.\.(.*)", block, re.DOTALL)
        if not m:
            continue
        label, body = m.group(1), m.group(2)
        if label not in wanted:
            continue
        body = " ".join(body.split())
        if body:
            raw[label] = body
    if not raw:
        return None

    pieces = []
    if event:
        pieces.append("%s %s is in effect" % (_indefinite_article(event), event))
    if "WHAT" in raw:
        pieces.append(raw["WHAT"])
    if "WHERE" in raw:
        pieces.append("This affects %s" % _lowercase_first_if_safe(raw["WHERE"]))
    if "WHEN" in raw:
        pieces.append("This is expected %s" % _lowercase_first_if_safe(raw["WHEN"]))
    if "IMPACTS" in raw:
        pieces.append(raw["IMPACTS"])
    return ". ".join(p.rstrip(".") for p in pieces) + "."

_REGION_HEADER_RE = re.compile(r"\n\s*IN\s+([A-Z][A-Z .'-]+?)\s*\n")

def _extract_watch_regions(description):
    if not description:
        return []
    seen = []
    for raw in _REGION_HEADER_RE.findall(description):
        region = " ".join(raw.split()).title()
        if region not in seen:
            seen.append(region)
    return seen

def _join_with_and(items):
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return "%s and %s" % (items[0], items[1])
    return "%s, and %s" % (", ".join(items[:-1]), items[-1])

def _extract_narrative_lead(description, area_desc=""):
    if not description:
        return None
    text = " ".join(description.split())
    cut_match = re.search(r"\bFOR THE FOLLOWING AREAS\b", text, re.IGNORECASE)
    if cut_match:
        lead = _sentence_case(text[:cut_match.end()].strip())
        if not lead:
            return None
        lead = _ensure_terminal_punctuation(lead)
        regions = _extract_watch_regions(description)
        if regions:
            lead += " This affects %s." % _join_with_and(regions)
        elif area_desc:
            lead += " This affects %s." % area_desc
        return lead
    cut_match = re.search(r"\bTHIS (?:WATCH|WARNING) INCLUDES\b", text, re.IGNORECASE)
    if cut_match:
        text = text[:cut_match.start()].strip()
    if not text:
        return None
    return _sentence_case(text)

def _ensure_terminal_punctuation(text):
    text = text.rstrip()
    if text and text[-1] not in ".!?":
        text += "."
    return text

def _strip_web_boilerplate(text):
    sentences = re.split(r"(?<=[.!?])\s+", text)
    kept = [s for s in sentences if not re.search(r"www\.|\.gov\b|\.com\b|http", s, re.IGNORECASE)]
    return " ".join(kept).strip()

_ALERT_SEVERITY_RANK = (
    ("warning",  3),
    ("watch",    2),
    ("advisory", 1),
    ("statement", 0),
)


def _alert_severity(entry):
    event = (entry.get("event") or "").lower()
    for keyword, rank in _ALERT_SEVERITY_RANK:
        if keyword in event:
            return rank
    return -1


def _normalized_body_key(entry):
    """Strip the leading 'An X is in effect.' sentence off text_core and
    normalize whitespace/case. Two entries whose bodies match after this
    transformation are NWS-sibling alerts (e.g. Extreme Heat Warning +
    Extreme Heat Watch active in the same area) whose descriptions are
    byte-identical past the opening declaration. Everything the listener
    needs to know about the sibling events (their names + per-event
    expirations) is already inside the shared body, so we can collapse
    them to one spoken block without losing information."""
    text = entry.get("text_core") or entry.get("text") or ""
    match = re.match(r"^[^.]*\.\s*", text)
    body = text[match.end():] if match else text
    return re.sub(r"\s+", " ", body).lower().strip()


def _dedup_similar_bodies(entries):
    """Collapse entries whose normalized bodies are identical. Keep
    highest-severity survivor per group. Preserves first-appearance
    order of surviving entries. Empty bodies never dedup (each is
    kept, assigned a unique key)."""
    if len(entries) <= 1:
        return entries

    from collections import OrderedDict
    groups = OrderedDict()
    for entry in entries:
        key = _normalized_body_key(entry)
        if not key:
            groups[("__empty__", len(groups))] = [entry]
        else:
            groups.setdefault(key, []).append(entry)

    result = []
    collapsed = 0
    for key, group in groups.items():
        if len(group) == 1:
            result.append(group[0])
        else:
            survivor = max(group, key=_alert_severity)
            dropped_names = [e.get("event", "?") for e in group if e is not survivor]
            log.info("wx dedup: '%s' body shared by %s; kept %s, dropped %s",
                     survivor.get("event", "?"),
                     ", ".join(dropped_names + [survivor.get("event", "?")]),
                     survivor.get("event", "?"),
                     ", ".join(dropped_names))
            result.append(survivor)
            collapsed += len(group) - 1

    if collapsed:
        log.info("wx dedup: collapsed %d duplicate-body entr%s",
                 collapsed, "y" if collapsed == 1 else "ies")
    return result


def build_watch_warning_text(props):
    """Returns {"text": <description+instruction>, "text_core": <description only>}
    or None if no usable content could be extracted.

    Two variants because two consumers need different verbosities:
    wx_alert.py uses `text` for its fingerprint-triggered urgent
    insert (full safety instructions included). wx_forecast.py uses
    `text_core` when it appends active alerts to the scheduled
    forecast -- listeners hear the same forecast rotation multiple
    times per day, and repeating the "Heat stroke is an emergency,
    call 9 1 1" tail every single time is fatiguing without adding
    new info (the safety tail is already in the urgent-insert clip).
    The description-only version keeps the who / what / where /
    until-when clauses that ARE genuinely useful on every airing."""
    description = props.get("description") or ""

    whats_happening = _extract_bullets(description, props.get("event", ""))
    if not whats_happening:
        whats_happening = _extract_convective_warning(description, props.get("event", ""))
    if not whats_happening:
        whats_happening = _extract_nws_headline(props)
    if not whats_happening:
        whats_happening = _extract_narrative_lead(description, props.get("areaDesc", ""))
    if not whats_happening:
        headline = props.get("headline")
        if headline:
            whats_happening = clean_alert_text(headline)

    if not whats_happening:
        return None

    def _tidy(s):
        s = _strip_web_boilerplate(s)
        s = re.sub(r"(\d+)\.0\b", r"\1", s)
        s = re.sub(r"\bCDT\b|\bCST\b", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s+([,.;:])", r"\1", s)
        s = normalize_times_for_tts(s)
        s = " ".join(s.split())
        return s

    core = _tidy(_ensure_terminal_punctuation(whats_happening))

    parts = [_ensure_terminal_punctuation(whats_happening)]
    instruction = props.get("instruction")
    if instruction:
        instruction = " ".join(instruction.split())
        if instruction:
            parts.append(_ensure_terminal_punctuation(instruction))
    full = _tidy(" ".join(parts))

    return {"text": full, "text_core": core}

def _trigger_current_temp():
    """Fire current_temp.py as a fire-and-forget subprocess so listeners
    get a fresh weather hit at the next WxTemp rotation slot, not on the
    next 15-minute cron tick. The script handles its own logging and
    uses a lockfile to serialize against any concurrent cron-driven run.

    stdout/stderr are inherited from THIS process (not discarded) so
    current_temp.py's own failure logging is actually observable
    (normally landing in the same journal/log destination this script's
    own output already goes to) -- this does not change the fire-and-
    forget/detached nature of the trigger at all: start_new_session=True
    still fully detaches the child (it survives this process exiting),
    inheriting a file descriptor is independent of session membership,
    and this process never waits on it either way."""
    if not os.path.exists(CURRENT_TEMP_SCRIPT):
        log.warning("Alert state changed but %s not found - skipping immediate broadcast trigger.",
                    CURRENT_TEMP_SCRIPT)
        return
    try:
        subprocess.Popen(
            [sys.executable, CURRENT_TEMP_SCRIPT, "--voice", "auto"],
            start_new_session=True,
        )
        log.info("Alert state changed - triggered current_temp.py for immediate broadcast.")
    except Exception as e:
        log.error("Failed to trigger current_temp.py: %s", e)


def _trigger_wx_alert():
    """Run wx_alert.py and WAIT for it to finish -- it delivers a fresh
    clip into the WxAlert category and fires engine.py's insert_urgent
    command, both of which need to land before this cron cycle
    considers the alert state "broadcast". A 120s timeout is generous
    for a few short clips + one ffmpeg concat; if it's ever hit,
    something is actually stuck.

    check=False is intentional -- a wx_alert.py failure must not raise
    here and disrupt this cycle's own ingestion/retry semantics (no
    retry storm), but its exit status IS now inspected and logged
    (previously discarded entirely) so a real synthesis/publication
    failure is observable rather than silently invisible."""
    if not os.path.exists(WX_ALERT_SCRIPT):
        log.warning("Alert state changed but %s not found - skipping wx alert insertion.",
                    WX_ALERT_SCRIPT)
        return
    try:
        result = subprocess.run([sys.executable, WX_ALERT_SCRIPT], check=False, timeout=120)
        if result.returncode != 0:
            log.error(
                "wx_alert.py exited %d (failure) -- watch/warning audio was NOT published "
                "this cycle; the previous wx_alert.mp3 (if any) remains in place.",
                result.returncode,
            )
        else:
            log.info("Triggered wx_alert.py for updated watch/warning audio.")
    except subprocess.TimeoutExpired:
        log.error("wx_alert.py timed out after 120s; proceeding without it this cycle.")
    except Exception as e:
        log.error("Failed to trigger wx_alert.py: %s", e)

_VTEC_PATTERN = re.compile(r"^/[OTX]\.(\w+)\.([A-Z]{4})\.([A-Z]{2})\.([A-Z])\.(\d{4})\.")

def _vtec_identity(props):
    params = props.get("parameters") or {}
    vtec_list = params.get("VTEC")
    if not isinstance(vtec_list, list) or not vtec_list:
        return None
    m = _VTEC_PATTERN.match(vtec_list[0].strip())
    if not m:
        return None
    _, office, phenomena, significance, tracking = m.groups()
    return (office, phenomena, significance, tracking)


def _alert_sent_dt(props):
    for key in ("sent", "effective", "onset"):
        val = props.get(key)
        if not val:
            continue
        try:
            return datetime.fromisoformat(val)
        except Exception:
            continue
    return None

def update_wx_alerts():
    try:
        r = session.get(ALERT_URL, timeout=10)
        if r.status_code != 200:
            return
        alerts = r.json().get("features", [])

        latest_by_event = {}
        severe_active = False
        for a in alerts:
            p = a.get("properties", {})
            event = p.get("event", "")
            headline = p.get("headline", "")
            if not headline:
                continue
            if any(k.lower() in event.lower() for k in ALERT_KEYWORDS):
                severe_active = True

            sent_dt = _alert_sent_dt(p)
            if _is_watch_or_warning(event):
                vtec_id = _vtec_identity(p)
                sub_key = vtec_id if vtec_id else p.get("areaDesc", "")
            else:
                sub_key = None
            dedup_key = (event, sub_key)
            existing = latest_by_event.get(dedup_key)
            if existing is None or (
                sent_dt is not None and (existing["sent"] is None or sent_dt > existing["sent"])
            ):
                latest_by_event[dedup_key] = {"event": event, "headline": headline, "sent": sent_dt, "props": p}

        all_alerts = [clean_alert_text(v["headline"]) for v in latest_by_event.values()]

        watchwarn_entries = []
        for info in latest_by_event.values():
            event = info["event"]
            if not _is_watch_or_warning(event):
                continue
            composed = build_watch_warning_text(info["props"])
            if not composed:
                log.warning("No usable text extracted for active %s; omitting from wx statement.", event)
                continue
            watchwarn_entries.append({
                "event": event,
                "text": composed["text"],
                "text_core": composed["text_core"],
            })
        watchwarn_entries = _dedup_similar_bodies(watchwarn_entries)
        safe_write_json(WX_WATCHWARN_FILE, watchwarn_entries)

        warn_text = ", ".join(all_alerts) + "\n" if all_alerts else ""
        safe_write_text(ALERT_DESCRIPTION, warn_text)
        if severe_active:
            safe_write_text(ALERT_STATUS, "Alert Active\n")
        elif os.path.exists(ALERT_STATUS):
            try:
                os.remove(ALERT_STATUS)
            except OSError as e:
                log.error("Could not remove %s: %s", ALERT_STATUS, e)

        new_fp = _alert_fingerprint(all_alerts)
        prior_fp = _read_prior_fingerprint()
        if new_fp != prior_fp:
            try:
                safe_write_text(ALERT_FINGERPRINT_FILE, new_fp + "\n")
            except Exception as e:
                log.error("Could not write alert fingerprint: %s", e)
            if prior_fp is not None:
                # wx_alert.py runs (and is waited on) FIRST so the live
                # queue insertion lands before current_temp.py's routine
                # short blurb goes out for this same cycle.
                _trigger_wx_alert()
                _trigger_current_temp()

        if VERBOSE:
            log.info("Updated alerts: %d total, severe active: %s",
                     len(all_alerts), severe_active)
    except Exception as e:
        log.error("Alert update error: %s", e)

# ---------------- RDS & PROCESSED WEATHER ----------------

def update_rds(data, trend):
    temp_f = float(data.get("tempf", 0))
    hum = float(data.get("humidity", 0))
    pres = float(data.get("baromrelin", 0))
    rain = float(data.get("dailyrainin", 0))

    try:
        with open(SMOOTHED_WIND_FILE, "r") as f:
            sw = json.load(f)
        wind = sw.get("speed", data.get("windspeedmph", 0))
        gust = sw.get("gust", data.get("windgustmph", 0))
        wdir = sw.get("dir", data.get("winddir", 0))
    except Exception:
        wind = data.get("windspeedmph", 0)
        gust = data.get("windgustmph", 0)
        wdir = data.get("winddir", 0)

    dew_point = calculate_dew_point(temp_f, hum)
    feels_like = calculate_feels_like(temp_f, hum, wind)

    data["feels_like"] = feels_like
    data["dew_point"] = round(dew_point, 1) if dew_point is not None else None

    symbol = {"rising rapidly": "++", "rising": "+", "falling": "-", "falling rapidly": "--"}.get(trend, " ")
    wtxt = wind_direction_to_compass(wdir)

    try:
        base = f"Temp: {round(temp_f)}F"
        feel = f" | Feels Like: {round(feels_like)}F" if round(feels_like) != round(temp_f) else ""
        humt = f" | Humidity: {round(hum)}%"
        extra = f" | Rain: {rain:.2f} in" if rain > 0 else f" | DewPt: {round(dew_point)}F"
        rds_temp_txt = f"{base}{feel}{humt}{extra}"

        wind_f = float(wind)
        gust_f = float(gust)
        if gust_f < 1:
            txt = "Wind: Calm"
        elif gust_f < 3:
            txt = "Wind: Light and Variable"
        else:
            wind_rounded = round(wind_f)
            gust_rounded = round(gust_f)
            gtxt = f", Gusts {gust_rounded}" if gust_rounded != wind_rounded else ""
            txt = f"Wind: {wtxt} at {wind_rounded}{gtxt}"
        rds_wind_txt = f"{txt} | Barometer: {pres:.2f} {symbol}"

        safe_write_text(RDS_FILE_1, rds_temp_txt)
        safe_write_text(RDS_FILE_2, rds_wind_txt)

        if VERBOSE:
            log.info("Updated RDS text files.")
    except Exception as e:
        log.error("Error writing RDS: %s", e)

    safe_write_json(PROCESSED_FILE, data)
    if VERBOSE:
        log.info("Updated processed weather file: %s", PROCESSED_FILE)

# ---------------- MAIN ----------------

def _main_body():
    if not os.path.exists(DATA_FILE):
        log.error("No weather data found at %s", DATA_FILE)
        return

    with open(DATA_FILE, "r") as f:
        data = json.load(f)

    pressure = float(data.get("baromrelin", 0))
    update_pressure_history(pressure)
    p_trend = get_pressure_trend(pressure)

    update_wx_alerts()
    update_rds(data, p_trend)

    now = datetime.now(timezone.utc)
    is_daytime = sun_above_horizon(LAT, LON, now)

    force = ("--no-cache" in sys.argv) or (os.environ.get("WX_NO_CACHE") == "1")

    nws_data, nws_avg, nws_newest_age_min = get_nws_cloud_cover(force_refresh=force)

    if nws_avg is not None and (nws_newest_age_min is not None and nws_newest_age_min <= NWS_RECENT_MIN):
        blended_cover = nws_avg
        used_source = "NWS"
    else:
        blended_cover = nws_avg
        used_source = "NWS_FALLBACK"

    condition = describe_sky_condition(blended_cover, is_daytime)

    wx_desc = None
    for v in (nws_data or {}).values():
        if v.get("nws_text") and v["nws_text"].lower() != "unknown":
            wx_desc = v["nws_text"]
            break
    rain_rate = data.get("rainratein", 0)
    condition = refine_sky_with_precip(condition, wx_desc, rain_rate)

    sky_result = {
        "time": now.isoformat(),
        "source_used": used_source,
        "nws_newest_age_min": nws_newest_age_min,
        "nws_avg": nws_avg,
        "blended_cover": blended_cover,
        "condition": condition,
        "night_mode": not is_daytime,
        "pressure_trend": p_trend
    }
    safe_write_json(SKY_FILE, sky_result)

    if VERBOSE:
        if nws_data:
            log.info("NWS station cloud observations:")
            for code, info in nws_data.items():
                frac = info.get("fraction")
                nws_text = info.get("nws_text", "Unknown")
                amounts = info.get("amounts", [])
                obs_time = info.get("obs_time")
                if frac is not None:
                    derived_label = describe_sky_condition(frac, is_daytime)
                    if obs_time:
                        log.info("  %s: %.1f%% -> %s (NWS: %s; layers: %s; time: %s)",
                                 code, frac*100, derived_label, nws_text, amounts, obs_time)
                    else:
                        log.info("  %s: %.1f%% -> %s (NWS: %s; layers: %s)",
                                 code, frac*100, derived_label, nws_text, amounts)
                else:
                    log.info("  %s: No cloud data (NWS: %s; layers: %s; time: %s)",
                             code, nws_text, amounts, obs_time)
        if nws_avg is not None:
            log.info("NWS average cloud cover: %.1f%% (newest age: %s min)",
                     nws_avg*100, nws_newest_age_min)
        log.info("Blended sky condition: %s [source=%s]", condition, used_source)
        log.info("Processed weather data written to %s", PROCESSED_FILE)
        log.info("=== Cycle complete ===")

def main():
    try:
        lock = acquire_lock(LOCKFILE)
    except RuntimeError as e:
        log.warning(str(e))
        return
    try:
        _main_body()
    finally:
        try:
            lock.close()
        except Exception:
            pass
        try:
            os.unlink(LOCKFILE)
        except Exception:
            pass

if __name__ == "__main__":
    main()
