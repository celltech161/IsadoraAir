#!/usr/bin/env python3
"""AMBER/BLU/MEP urgent-insert audio delivery.

Triggered synchronously by amber_poll.py whenever the active alert
fingerprint changes and the surviving set is non-empty. Mirrors
wx_alert.py's shape:

  read active_amber_alerts.json
      -> synthesize each alert's `text` field via the canonical shared
         IsadoraAir TTS CLI (on-duty logical voice picked by weather
         config's voice_schedule -- see lib/voices.py)
      -> concatenate + convert to MP3
      -> deliver to /srv/isadoraair/music/WxAlert/wx_alert.mp3 via
         lib/delivery.py
      -> fire engine.py's insert_urgent command for WxAlert category

Reuses the WxAlert category by design (per operator decision) so the
on-air flow is identical to weather alerts -- no new category, no new
dashboard concept, no new log-builder branch.
"""

import fcntl
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from delivery import deliver  # noqa: E402
import voices  # noqa: E402
from voices import VoiceResolutionError, resolve_voice  # noqa: E402
from wxconfig import load_weather_config, resolve_weather_data_dir  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [amber_alert] %(message)s",
)
log = logging.getLogger(__name__)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = resolve_weather_data_dir()
ACTIVE_FILE = DATA_DIR / "active_amber_alerts.json"

TMP_DIR = "/tmp/amber-alert"
COMBINED_OUTPUT = os.path.join(TMP_DIR, "wx_alert.mp3")

# Reuse WxAlert category deliberately -- same on-air pattern as weather
# alerts. See AmberAlertConfig docstring's design decision.
CATEGORY_CODE = "WxAlert"
DEST_FILENAME = "wx_alert.mp3"

ENGINE_CMD_PATH = Path("/run/isadoraair/engine_cmd.json")
LOCKFILE = "/tmp/amber-alert.lock"


def _acquire_lock(path):
    f = open(path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError("amber_alert: another instance is running.")
    f.write(str(os.getpid()))
    f.flush()
    return f


def _load_active():
    if not ACTIVE_FILE.exists():
        return []
    try:
        data = json.loads(ACTIVE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("Could not read %s: %s", ACTIVE_FILE, exc)
        return []
    return data if isinstance(data, list) else []


def _concat_clips(clip_paths, output_path):
    """Same ffmpeg concat approach wx_alert.py uses, kept identical so
    the on-air perceived timing/level is consistent between weather
    and AMBER urgent inserts."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    list_path = output_path + ".list.txt"

    def esc(path):
        return path.replace("'", "'\\''")

    try:
        with open(list_path, "w", encoding="utf-8") as f:
            for path in clip_paths:
                f.write("file '%s'\n" % esc(path))
    except Exception as exc:
        log.error("Failed to write concat list %s: %s", list_path, exc)
        return False

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-y", "-f", "concat", "-safe", "0",
        "-i", list_path,
        # 1s of leading silence so the on-air chain can fade/cue in
        # cleanly -- matches wx_alert's setting exactly.
        "-af", "adelay=1000:all=1",
        "-acodec", "libmp3lame",
        "-ar", "44100", "-ac", "2", "-b:a", "320k",
        "-metadata", "artist=AMBER Alert",
        "-metadata", "title=AMBER Alert",
        output_path,
    ]
    try:
        subprocess.run(cmd, check=True)
        return True
    except subprocess.CalledProcessError as exc:
        log.error("ffmpeg concat failed building %s: %s", output_path, exc)
        return False
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


def _fire_insert_urgent():
    ENGINE_CMD_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENGINE_CMD_PATH.write_text(
        json.dumps({"command": "insert_urgent", "category": CATEGORY_CODE}),
        encoding="utf-8",
    )
    log.info("Fired insert_urgent command for category %s", CATEGORY_CODE)


def _main_body():
    """All-or-nothing at the synthesis-set level -- mirrors wx_alert.py's
    own _main_body() exactly (see that function's docstring): any
    failed segment aborts the whole publication attempt before concat/
    delivery/insert_urgent, leaving the previous final wx_alert.mp3
    untouched. Returns True on success (including the benign "nothing
    to insert" no-op), False on a real failure that left nothing
    published."""
    entries = _load_active()
    qualifying = [e for e in entries if e.get("text")]

    if not qualifying:
        log.info("No active AMBER/BLU/MEP alerts with usable text; nothing to insert.")
        return True

    try:
        cfg = load_weather_config()
        slot, voice = resolve_voice(cfg, "auto")
    except VoiceResolutionError as exc:
        log.error("Voice resolution failed: %s; aborting -- no clips synthesized.", exc)
        return False
    log.info("Building %d AMBER-family clip(s) with voice=%s", len(qualifying), voice["name"])

    os.makedirs(TMP_DIR, exist_ok=True)
    clip_paths = []
    for i, entry in enumerate(qualifying):
        wav_path = os.path.join(TMP_DIR, "clip_%02d.wav" % i)
        result = voices.synthesize(entry["text"], wav_path, voice)
        if not result:
            # Atomic at the synthesis-set level -- one failed segment
            # aborts the WHOLE publication attempt; see wx_alert.py's
            # own identical comment.
            log.error(
                "Synthesis failed for %s (%s) -- aborting entire AMBER publication; "
                "the previous wx_alert.mp3 (if any) is left untouched.",
                entry.get("event", "?"), result.reason,
            )
            return False
        clip_paths.append(wav_path)
        log.info("Synthesized: %s -> %s", entry.get("event", "?"), wav_path)

    if not _concat_clips(clip_paths, COMBINED_OUTPUT):
        log.error("Concat failed; nothing to insert. Previous wx_alert.mp3 (if any) is untouched.")
        return False

    dest = deliver(
        COMBINED_OUTPUT, CATEGORY_CODE, DEST_FILENAME,
        producer="amber_alert.py", voice=voice["name"],
        source_kind="event", alert_family="ipaws_amber",
    )
    log.info("Delivered and synced: %s", dest)

    _fire_insert_urgent()
    log.info("=== amber_alert complete ===")
    return True


def main():
    """Returns True on success (including benign no-ops), False on a
    real publication failure."""
    try:
        lock = _acquire_lock(LOCKFILE)
    except RuntimeError as exc:
        log.warning(str(exc))
        return True
    ok = True
    try:
        ok = _main_body()
    except Exception:
        log.exception("Fatal error in amber_alert")
        ok = False
    finally:
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
