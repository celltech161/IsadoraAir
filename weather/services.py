"""GW3000/Ecowitt weather-gateway ingestion + wind smoothing.

Ported from /home/jreed/wx_scripts/app.py (Flask receiver) and
wind_smoother.py (kicked as a subprocess on every POST). Folded into
Django as plain function calls instead of a second web service + a
subprocess-per-POST: both are cheap, pure-Python, sub-millisecond
operations over small JSON files, so there's nothing here that
benefits from a separate process the way Piper/ffmpeg-based work does.

Data files live under DATA_DIR, read/written by both this module (via
the Django view, running as the gunicorn user) and the standalone
cron scripts under /home/jreed/weather-ingest/ (running as the same
user) -- same shared-directory convention as syndicated-ingest/lib.
"""

import json
import math
import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA_DIR = Path("/home/jreed/weather-ingest/data")

LATEST_WEATHER_FILE = DATA_DIR / "latest_weather.json"
WIND_HISTORY_FILE = DATA_DIR / "wind_history.json"
SMOOTHED_WIND_FILE = DATA_DIR / "smoothed_wind.json"

WIND_HISTORY_WINDOW_MINUTES = 30

# Rolling-window wind smoothing. Sustained "speed" is the MAXIMUM over
# the window, not the mean -- a deliberate calibration choice for this
# sensor's specific (partially obstructed) site, validated empirically
# over ~1 year of observation on kogr-sc. Do NOT change to a mean
# without re-validating against on-site conditions; see
# wind_smoother.py's original comment for the full rationale, carried
# forward unchanged here.
SMOOTH_WINDOW_MINUTES = 10
SMOOTH_WEIGHT_WINDOW = 5

_wind_history_lock = threading.Lock()

DATA_DIR.mkdir(parents=True, exist_ok=True)


def safe_write_json(path, obj):
    """Atomic write: unique tmp path per (process, thread) -> os.replace.
    Prevents a reader from ever observing a truncated/partial file."""
    tmp_path = Path(f"{path}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(obj, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def record_gateway_payload(payload):
    """Write latest_weather.json and append to the rolling wind history.
    Returns the payload with its injected timestamp."""
    payload = dict(payload)
    payload["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    safe_write_json(LATEST_WEATHER_FILE, payload)
    _update_wind_history(payload)
    return payload


def _update_wind_history(payload):
    now = datetime.now(timezone.utc)
    entry = {
        "time": now.isoformat(),
        "dir": float(payload.get("winddir", 0)),
        "speed": float(payload.get("windspeedmph", 0)),
        "gust": float(payload.get("windgustmph", 0)),
    }

    # The lock spans the full read-filter-append-write cycle -- narrower
    # scope would reopen the race this exists to prevent (two concurrent
    # gateway POSTs both reading, each appending, second write clobbering
    # the first's append).
    with _wind_history_lock:
        history = []
        if WIND_HISTORY_FILE.exists():
            try:
                history = json.loads(WIND_HISTORY_FILE.read_text())
            except Exception:
                history = []

        cutoff = now - timedelta(minutes=WIND_HISTORY_WINDOW_MINUTES)
        filtered = []
        for h in history:
            try:
                t = datetime.fromisoformat(h["time"].replace("Z", "+00:00"))
                if t >= cutoff:
                    filtered.append(h)
            except Exception:
                continue

        filtered.append(entry)
        safe_write_json(WIND_HISTORY_FILE, filtered)


def _average_wind_direction(samples, weights):
    if not samples:
        return 0.0
    total_weight = sum(weights) if weights else 0.0
    if total_weight <= 0.0:
        weights = [1.0] * len(samples)
        total_weight = float(len(samples))
    sin_sum = cos_sum = 0.0
    for s, w in zip(samples, weights):
        rad = math.radians(s)
        sin_sum += math.sin(rad) * w
        cos_sum += math.cos(rad) * w
    return math.degrees(math.atan2(sin_sum / total_weight, cos_sum / total_weight)) % 360


def smooth_wind():
    """Recompute smoothed_wind.json from the current wind_history.json.
    Called synchronously right after every gateway POST -- cheap enough
    (a handful of JSON list entries) that there's no need for the
    background-thread dance the original Flask receiver used to avoid
    blocking the POST response."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=SMOOTH_WINDOW_MINUTES)

    try:
        history = json.loads(WIND_HISTORY_FILE.read_text())
    except Exception:
        history = []
    if not history:
        return None

    timed_samples = []
    for entry in history:
        try:
            t = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
        except Exception:
            continue
        if t >= cutoff:
            timed_samples.append((t, entry))
    if not timed_samples:
        return None

    weights = []
    for t, _entry in timed_samples:
        age_min = (now - t).total_seconds() / 60.0
        if age_min <= SMOOTH_WEIGHT_WINDOW:
            weight = 1.0
        elif age_min <= SMOOTH_WINDOW_MINUTES:
            denom = SMOOTH_WINDOW_MINUTES - SMOOTH_WEIGHT_WINDOW
            weight = max(0.0, (SMOOTH_WINDOW_MINUTES - age_min) / denom) if denom > 0 else 0.0
        else:
            weight = 0.0
        weights.append(weight)

    samples = [entry for _t, entry in timed_samples]
    speeds = [float(s.get("speed", 0)) for s in samples]
    gusts = [float(s.get("gust", 0)) for s in samples]
    dirs = [float(s.get("dir", 0)) for s in samples]

    smoothed = {
        "time": now.isoformat().replace("+00:00", "Z"),
        "speed": round(max(speeds), 1) if speeds else 0.0,
        "gust": round(max(gusts), 1) if gusts else 0.0,
        "dir": round(_average_wind_direction(dirs, weights), 1) if dirs else 0.0,
    }
    safe_write_json(SMOOTHED_WIND_FILE, smoothed)
    return smoothed
