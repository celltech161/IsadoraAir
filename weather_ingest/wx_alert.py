#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Combined Weather/AMBER spoken-alert coordinator
# ------------------------------------------------
# The ONE producer of WxAlert/wx_alert.flac.  Both pollers run it (and wait
# for it) after every cycle -- update_local_wx_data.py (NWS watches/
# warnings) and amber_poll.py (AMBER/BLU/MEP) -- and amber_alert.py is now
# only a compatibility wrapper around it.  Each run, under one coordinator
# lock:
#
#   1. reads BOTH source snapshots (active_watches_warnings.json,
#      active_amber_alerts.json) and drops every entry whose authoritative
#      expiration has passed (so known expirations apply during a feed
#      outage) or that carries no expiration at all (validity cannot be
#      established -> conservative exclusion, logged);
#   2. fingerprints the EFFECTIVE spoken content -- source, alert identity
#      and the exact text that would be spoken, in a deterministic order
#      (NWS first, most severe first; then AMBER) -- never headlines,
#      advisories or feed timestamps;
#   3. compares it with WEATHER_DATA_DIR/wx_alert_state.json
#      (isadoraair/wx_alert_state.py) and:
#        * unchanged  -> no synthesis, no publication; while an
#          announcement is active, re-send the idempotent engine nudge (the
#          engine queues nothing for an already-queued/claimed generation);
#        * empty      -> mark the announcement inactive (a new generation):
#          the engine refuses to start any pending occurrence;
#        * changed    -> synthesize every clip, concatenate one calibrated
#          FLAC, re-check that the sources did not change meanwhile,
#          publish through publish_weather_asset (atomic, last-known-good),
#          record the new generation, then nudge the engine.
#   A failure at any step leaves the recorded generation unchanged, so the
#   next poll cycle retries; a failed nudge is retried by the next cycle's
#   unchanged-state nudge.
#
# First run (no state file): the current effective content is recorded as
# an un-announced baseline -- the pre-existing "first run is silent" rule --
# and every later genuine change is announced.
#
# Author: Justin Reed

import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from isadoraair import wx_alert_state
from isadoraair.engine_commands import enqueue_engine_command
import delivery
from delivery import deliver
import speech_audio
import voices
from voices import VoiceResolutionError, resolve_voice
from wxconfig import load_weather_config, resolve_weather_data_dir

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [wx_alert] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------- CONFIGURATION ----------------

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = resolve_weather_data_dir()

WX_WATCHWARN_FILE = DATA_DIR / "active_watches_warnings.json"
AMBER_FILE = DATA_DIR / "active_amber_alerts.json"
STATE_FILE = wx_alert_state.state_path(DATA_DIR)
# In the shared data directory (not /tmp), and never unlinked: a waiter
# must always contend on the same inode as the holder.
LOCKFILE = DATA_DIR / ".wx_alert_coordinator.lock"
LOCK_WAIT_SECONDS = 150

TMP_DIR = "/tmp/syndicated-wx-alert"
COMBINED_OUTPUT = os.path.join(TMP_DIR, "wx_alert.flac")

CATEGORY_CODE = wx_alert_state.CATEGORY_CODE
DEST_FILENAME = wx_alert_state.AUDIO_FILENAME
TITLE = "Weather Alert"

SOURCE_ORDER = ("nws", "amber")


class CoordinatorLockTimeout(RuntimeError):
    pass


def acquire_lock(path, wait_seconds=LOCK_WAIT_SECONDS):
    """Blocking (bounded) exclusive lock: a second run waits for the first
    and then decides from the NEWEST snapshots, instead of being dropped."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.monotonic() >= deadline:
                f.close()
                raise CoordinatorLockTimeout(f"coordinator lock not obtained within {wait_seconds}s")
            time.sleep(0.5)

# ---------------- SOURCES ----------------

LEGACY = "legacy"


def read_snapshot(path, source=None):
    """(entries, status): status "ok" for a readable list, "missing" for a
    source that has never produced a snapshot (no known alerts),
    "malformed: ..." with entries None (unknown -- nothing may change), or
    LEGACY with entries None: an NWS snapshot written before r0137 (entries
    without identity/validity), which the next NWS poll rewrites."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return [], "missing"
    except Exception as e:
        return None, f"malformed: {e}"
    if not isinstance(data, list):
        return None, "malformed: not a list"
    if source == "nws" and any(isinstance(e, dict) and "key" not in e for e in data):
        return None, LEGACY
    return data, "ok"


def _parse_deadline(value):
    parsed = wx_alert_state.parse_ts(value) if isinstance(value, str) else None
    return parsed


def _entry_deadline(source, entry):
    """The authoritative end of validity, or None when the source gave none."""
    if source == "nws":
        candidates = [_parse_deadline(entry.get("expires")), _parse_deadline(entry.get("ends"))]
    else:
        candidates = [_parse_deadline(entry.get("expires"))]
    candidates = [c for c in candidates if c is not None]
    return min(candidates) if candidates else None


def _entry_key(source, entry):
    if source == "nws":
        return str(entry.get("key") or entry.get("event") or "")
    return str(entry.get("identifier") or entry.get("event") or "")


def effective_alerts(now):
    """(alerts, sources, excluded). alerts is None when any snapshot is
    unreadable (nothing may change until it is readable again)."""
    sources = {}
    raw = {}
    for source, path in (("nws", WX_WATCHWARN_FILE), ("amber", AMBER_FILE)):
        entries, status = read_snapshot(path, source)
        sources[source] = status
        raw[source] = entries
    if any(entries is None for entries in raw.values()):
        return None, sources, []

    alerts, excluded = [], []
    for source in SOURCE_ORDER:
        picked = []
        for entry in raw[source]:
            if not isinstance(entry, dict):
                continue
            text = (entry.get("text") or "").strip()
            if not text:
                continue
            key = _entry_key(source, entry)
            deadline = _entry_deadline(source, entry)
            if deadline is None:
                excluded.append((source, key, "no authoritative expiration"))
                continue
            if deadline <= now:
                excluded.append((source, key, f"expired {wx_alert_state.format_ts(deadline)}"))
                continue
            picked.append({
                "source": source, "key": key, "event": entry.get("event") or "",
                "text": text, "deadline": deadline,
            })
        if source == "amber":
            # Feed order is not meaningful; NWS order is already the
            # poller's deterministic severity order.
            picked.sort(key=lambda a: (a["event"], a["key"]))
        alerts.extend(picked)
    for source, key, why in excluded:
        log.warning("Excluded %s alert %s from the spoken announcement: %s", source, key, why)
    return alerts, sources, excluded


def content_fingerprint(alerts):
    if not alerts:
        return ""
    canonical = json.dumps([[a["source"], a["key"], a["text"]] for a in alerts], separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _alert_summary(alerts):
    return [
        {"source": a["source"], "key": a["key"], "event": a["event"],
         "deadline": wx_alert_state.format_ts(a["deadline"])}
        for a in alerts
    ]

# ---------------- RENDERING ----------------

def synthesize_clip(text, voice, out_path):
    """Synthesis itself is voices.synthesize()'s job, via the canonical
    shared TTS CLI."""
    return voices.synthesize(text, out_path, voice)


def concat_clips(clip_paths, output_path, voice=None):
    """Concatenate the per-alert WAV clips into one calibrated FLAC via ffmpeg."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    list_path = output_path + ".list.txt"

    def esc(path):
        return path.replace("'", "'\\''")

    try:
        with open(list_path, "w", encoding="utf-8") as f:
            for path in clip_paths:
                f.write("file '%s'\n" % esc(path))
    except Exception as e:
        log.error("Failed to write concat list %s: %s", list_path, e)
        return False

    try:
        # One calibrated encode for the whole concatenated alert: gain + peak limiter, then the 1 s lead-in
        # once ahead of everything (see lib/speech_audio.py).
        speech_audio.encode_flac(
            ["-f", "concat", "-safe", "0", "-i", list_path], output_path,
            speech_audio.gain_db_for("alert", voice),
            {"artist": TITLE, "title": TITLE},
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        log.error("ffmpeg concat failed building %s: %s", output_path, e)
        return False
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


def fire_insert_urgent():
    """Idempotent synchronization nudge: the engine reads the current state
    record itself, so a delayed or repeated nudge can only converge on the
    newest generation."""
    enqueue_engine_command(
        {"command": "insert_urgent", "category": CATEGORY_CODE}
    )
    log.info("Queued insert_urgent synchronization for category %s", CATEGORY_CODE)


def render(alerts):
    """All-or-nothing: every clip must synthesize, or nothing is
    concatenated/published. Returns (output_path, voice) or (None, None)."""
    try:
        cfg = load_weather_config()
        slot, voice = resolve_voice(cfg, "auto")
    except VoiceResolutionError as e:
        log.error("Voice resolution failed: %s; aborting -- no clips synthesized.", e)
        return None, None
    log.info("Building %d alert clip(s) with voice=%s", len(alerts), voice["name"])

    os.makedirs(TMP_DIR, exist_ok=True)
    clip_paths = []
    for i, alert in enumerate(alerts):
        wav_path = os.path.join(TMP_DIR, "clip_%02d.wav" % i)
        result = synthesize_clip(alert["text"], voice, wav_path)
        if not result:
            log.error(
                "Synthesis failed for %s %s (%s) -- aborting the whole announcement; "
                "the previous wx_alert.flac (if any) is left untouched.",
                alert["source"], alert["event"] or alert["key"], result.reason,
            )
            return None, None
        clip_paths.append(wav_path)
        log.info("Synthesized: %s %s -> %s", alert["source"], alert["event"] or alert["key"], wav_path)

    if not concat_clips(clip_paths, COMBINED_OUTPUT, voice):
        log.error("Concat failed; nothing published. Previous wx_alert.flac (if any) is untouched.")
        return None, None
    return COMBINED_OUTPUT, voice

# ---------------- STATE ----------------

def _write(state, **changes):
    new = dict(state or {})
    new.update(changes)
    new["updated_at"] = wx_alert_state.format_ts(wx_alert_state.now_utc())
    wx_alert_state.write_state(STATE_FILE, new)
    return new


def _note_error(state, message):
    """Record a failure for diagnostics WITHOUT advancing the generation."""
    if state is None:
        return
    try:
        _write(state, last_error=message)
    except Exception as e:
        log.error("Could not record coordinator error in %s: %s", STATE_FILE, e)


def _refresh_observations(state, base):
    """Keep the diagnostic source/exclusion observations current without
    touching the generation."""
    if any(state.get(k) != v for k, v in base.items()) or state.get("last_error"):
        _write(state, last_error=None, **base)


def coordinate(now=None):
    """One coordinator pass (caller holds the lock). Returns True on success
    (including every benign no-op), False on a failure the next cycle retries."""
    now = now or wx_alert_state.now_utc()
    state, state_error = wx_alert_state.read_state(STATE_FILE)
    alerts, sources, excluded = effective_alerts(now)
    if alerts is None and LEGACY in sources.values():
        # Upgrade window: the NWS snapshot predates r0137. Deciding now would
        # drop its alerts (no expirations) and announce them moments later,
        # breaking the first-run baseline; the next NWS poll rewrites it.
        log.info("NWS snapshot predates the combined coordinator; waiting for the next NWS poll.")
        return True
    if alerts is None:
        message = f"source snapshot unreadable: {sources}"
        log.error("%s -- the announcement state is left unchanged.", message)
        _note_error(state, message)
        return False

    fingerprint = content_fingerprint(alerts)
    base = {"sources": sources, "excluded": [list(x) for x in excluded]}

    if state is None and state_error == "missing":
        _write(None, generation=0, fingerprint=fingerprint, active=False, baseline=True,
               alerts=_alert_summary(alerts), valid_until=None, published_at=None, audio=None,
               last_error=None, **base)
        log.info("First run: recorded the current alert content (%d alert(s)) as an un-announced baseline.",
                 len(alerts))
        return True
    if state is None:
        log.error("Alert state %s is %s; rebuilding it from the current sources.", STATE_FILE, state_error)
        state = {"generation": 0, "fingerprint": None, "active": False, "baseline": False}

    generation = int(state.get("generation") or 0)

    if not alerts:
        if state.get("active") or state.get("fingerprint") != "":
            _write(state, generation=generation + 1, fingerprint="", active=False, baseline=False,
                   alerts=[], valid_until=None, published_at=None, audio=None, last_error=None, **base)
            log.info("No qualifying alert is in effect: announcement generation %d is inactive; "
                     "no pending occurrence may start.", generation + 1)
        else:
            _refresh_observations(state, base)
        return True

    if fingerprint == state.get("fingerprint") and (state.get("active") or state.get("baseline")):
        if not state.get("active"):
            _refresh_observations(state, base)
            return True  # first-run baseline content, never announced
        published = wx_alert_state.audio_identity(delivery.LIBRARY_ROOT / CATEGORY_CODE / DEST_FILENAME)
        expected = state.get("audio") or {}
        if published is not None and (published["size"], published["mtime_ns"]) == (
            expected.get("size"), expected.get("mtime_ns")
        ):
            summary = _alert_summary(alerts)
            valid_until = wx_alert_state.format_ts(min(a["deadline"] for a in alerts))
            if summary != state.get("alerts") or valid_until != state.get("valid_until") or state.get("last_error"):
                state = _write(state, alerts=summary, valid_until=valid_until, last_error=None, **base)
            try:
                fire_insert_urgent()
            except Exception as e:
                log.error("Engine synchronization nudge failed (%s); retried next cycle.", e)
                return False
            return True
        log.warning("Published wx_alert.flac no longer matches generation %d; republishing.", generation)

    return _publish(state, generation, alerts, fingerprint, base, now)


def _publish(state, generation, alerts, fingerprint, base, now=None):
    output, voice = render(alerts)
    if output is None:
        _note_error(state, "synthesis failed")
        return False

    # Stale-render guard: the sources may have changed while we synthesized.
    # (An alert expiring meanwhile is caught by valid_until at the engine and
    # by the next pass.)
    latest, _sources, _excluded = effective_alerts(now or wx_alert_state.now_utc())
    if latest is None or content_fingerprint(latest) != fingerprint:
        log.info("Alert sources changed during synthesis; this candidate is discarded unpublished -- "
                 "the next coordinator pass publishes the latest content.")
        return True

    try:
        dest = deliver(
            output, CATEGORY_CODE, DEST_FILENAME,
            producer="wx_alert.py", voice=voice["name"],
            source_kind="event", alert_family=_alert_family(alerts),
        )
    except Exception as e:
        log.error("Publication failed (%s); the previous announcement state is unchanged and retried next cycle.", e)
        _note_error(state, f"publication failed: {e}")
        return False
    identity = wx_alert_state.audio_identity(dest)
    if identity is None:
        log.error("Publication reported success but %s is missing; retried next cycle.", dest)
        _note_error(state, "published file missing after publication")
        return False

    new_generation = generation + 1
    _write(state, generation=new_generation, fingerprint=fingerprint, active=True, baseline=False,
           alerts=_alert_summary(alerts),
           valid_until=wx_alert_state.format_ts(min(a["deadline"] for a in alerts)),
           published_at=wx_alert_state.format_ts(wx_alert_state.now_utc()),
           audio=identity, last_error=None, **base)
    log.info("Published announcement generation %d (%d alert(s): %s) to %s", new_generation, len(alerts),
             ", ".join(f"{a['source']}:{a['event'] or a['key']}" for a in alerts), dest)
    try:
        fire_insert_urgent()
    except Exception as e:
        log.error("Engine synchronization nudge failed (%s); retried next cycle.", e)
        return False
    return True


def _alert_family(alerts):
    families = {a["source"] for a in alerts}
    if families == {"nws"}:
        return "nws_watch_warning"
    if families == {"amber"}:
        return "ipaws_amber"
    return "combined"

# ---------------- MAIN ----------------

def main():
    """Returns True on success (including benign no-ops), False on a real
    failure (the next poll cycle retries)."""
    try:
        lock = acquire_lock(LOCKFILE)
    except CoordinatorLockTimeout as e:
        log.error("%s; the next poll cycle retries.", e)
        return False
    ok = True
    try:
        ok = coordinate()
    except Exception:
        log.exception("Fatal error in the alert coordinator")
        ok = False
    finally:
        # TMP_DIR is exclusive to this script -- /tmp is tmpfs
        # (RAM-backed) on this box.
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        try:
            lock.close()
        except Exception:
            pass
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
