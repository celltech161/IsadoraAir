#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# WX Watch/Warning Alert -- live queue insertion
# ------------------------------------------------
# Reads active_watches_warnings.json (written by update_local_wx_data.py),
# synthesizes one clip per active Watch/Warning via the canonical shared
# IsadoraAir TTS CLI (see lib/voices.py), concatenates
# them into a single clip, delivers it to the WxAlert category, and
# fires engine.py's insert_urgent command so it plays at the very next
# track boundary -- live-verified against production.
#
# Ported from /home/jreed/wx_scripts/wx_alert_statement.py, which built
# an "urgent_pa" mix via ogremote_processor_lib -- that PA break-in
# system was never ported to this box (separate scope from the weather
# pipeline), so this replaces it with IsadoraAir's own local-delivery +
# live-queue-insertion mechanism instead, which is both simpler and
# faster (seconds, not "wait for the next scheduled slot").
#
# Triggered synchronously by update_local_wx_data.py immediately
# whenever the active alert set changes -- this script is expected to
# run to completion (not fire-and-forget) so the insertion has actually
# happened before the cron cycle considers the alert broadcast.
#
# Author: Justin Reed

import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from delivery import deliver
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

TMP_DIR = "/tmp/syndicated-wx-alert"
COMBINED_OUTPUT = os.path.join(TMP_DIR, "wx_alert.mp3")

CATEGORY_CODE = "WxAlert"
DEST_FILENAME = "wx_alert.mp3"

ENGINE_CMD_PATH = Path("/run/isadoraair/engine_cmd.json")

LOCKFILE = "/tmp/syndicated-wx-alert.lock"

def acquire_lock(path):
    f = open(path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        f.write(str(os.getpid()))
        f.flush()
        return f
    except BlockingIOError:
        f.close()
        raise RuntimeError("Another instance is already running.")

# ---------------- HELPERS ----------------

def load_watchwarn_entries():
    if not WX_WATCHWARN_FILE.exists():
        log.info("No %s found; treating as no active watches/warnings.", WX_WATCHWARN_FILE)
        return []
    try:
        with open(WX_WATCHWARN_FILE, "r") as f:
            data = json.load(f)
    except Exception as e:
        log.error("Could not read/parse %s: %s", WX_WATCHWARN_FILE, e)
        return []
    if not isinstance(data, list):
        log.error("%s did not contain a list; ignoring.", WX_WATCHWARN_FILE)
        return []
    return data


def synthesize_clip(text, voice, out_path):
    """Preserves the original name/signature; synthesis itself is
    voices.synthesize()'s job, via the canonical shared TTS CLI."""
    return voices.synthesize(text, out_path, voice)


def concat_clips(clip_paths, output_path):
    """Concatenate the per-alert WAV clips into one MP3 via ffmpeg."""
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

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-y", "-f", "concat", "-safe", "0",
        "-i", list_path,
        # Piper's own audio starts right at sample 0 with no lead-in, which
        # reads as clipped on air -- 1s of silence up front (once, ahead of
        # the whole concatenated clip) gives the on-air chain room to
        # fade/cue in cleanly.
        "-af", "adelay=1000:all=1",
        "-acodec", "libmp3lame",
        "-ar", "44100", "-ac", "2", "-b:a", "320k",
        "-metadata", "artist=Weather Alert",
        "-metadata", "title=Weather Alert",
        output_path,
    ]
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as e:
        log.error("ffmpeg concat failed building %s: %s", output_path, e)
        return False
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


def fire_insert_urgent():
    ENGINE_CMD_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENGINE_CMD_PATH.write_text(
        json.dumps({"command": "insert_urgent", "category": CATEGORY_CODE}),
        encoding="utf-8",
    )
    log.info("Fired insert_urgent command for category %s", CATEGORY_CODE)


# ---------------- MAIN ----------------

def _main_body():
    """All-or-nothing at the synthesis-set level: if ANY qualifying
    entry fails to synthesize, this aborts before concat/delivery/
    insert_urgent entirely -- the previous final wx_alert.mp3 (if any)
    is left completely untouched, and nothing is published from an
    incomplete clip set. Only when EVERY qualifying entry synthesizes
    successfully does concatenation/delivery/insertion proceed.
    Returns True on success (including the benign "nothing to insert"
    no-op), False on a real failure that left nothing published."""
    entries = load_watchwarn_entries()
    qualifying = [e for e in entries if e.get("text")]

    if not qualifying:
        log.info("No active watches/warnings with usable text; nothing to insert.")
        return True

    try:
        cfg = load_weather_config()
        slot, voice = resolve_voice(cfg, "auto")
    except VoiceResolutionError as e:
        log.error("Voice resolution failed: %s; aborting -- no clips synthesized.", e)
        return False
    log.info("Building %d watch/warning clip(s) with voice=%s", len(qualifying), voice["name"])

    os.makedirs(TMP_DIR, exist_ok=True)
    clip_paths = []
    for i, entry in enumerate(qualifying):
        wav_path = os.path.join(TMP_DIR, "clip_%02d.wav" % i)
        result = synthesize_clip(entry["text"], voice, wav_path)
        if not result:
            # Atomic at the synthesis-set level: one failed segment
            # aborts the WHOLE publication attempt -- no concat, no
            # ffmpeg on the incomplete subset, no delivery, no
            # insert_urgent. The previous final file is untouched.
            log.error(
                "Synthesis failed for %s (%s) -- aborting entire alert publication; "
                "the previous wx_alert.mp3 (if any) is left untouched.",
                entry.get("event", "?"), result.reason,
            )
            return False
        clip_paths.append(wav_path)
        log.info("Synthesized: %s -> %s", entry.get("event", "?"), wav_path)

    if not concat_clips(clip_paths, COMBINED_OUTPUT):
        log.error("Concat failed; nothing to insert. Previous wx_alert.mp3 (if any) is untouched.")
        return False

    dest = deliver(COMBINED_OUTPUT, CATEGORY_CODE, DEST_FILENAME)
    log.info("Delivered and synced: %s", dest)

    fire_insert_urgent()
    log.info("=== wx_alert complete ===")
    return True


def main():
    """Returns True on success (including benign no-ops: nothing to
    insert, or another instance already running), False on a real
    publication failure."""
    try:
        lock = acquire_lock(LOCKFILE)
    except RuntimeError as e:
        log.warning(str(e))
        return True
    ok = True
    try:
        ok = _main_body()
    except Exception:
        log.exception("Fatal error in wx_alert")
        ok = False
    finally:
        # TMP_DIR is exclusive to this script -- /tmp is tmpfs
        # (RAM-backed) on this box.
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        try:
            lock.close()
        except Exception:
            pass
        try:
            os.unlink(LOCKFILE)
        except Exception:
            pass
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
