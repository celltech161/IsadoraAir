#!/usr/bin/env python3
"""IPAWS OPEN poller for AMBER (CAE) / Blue (BLU) / Missing-Endangered
(MEP) alerts. Runs on the amber-alert-poll.timer cadence.

Pipeline (matches wx_alert.py's fingerprint-triggered pattern):

  1. Config-gated: exits immediately if AmberAlertConfig.enabled=False.
  2. Fetch IPAWS OPEN /rest/feed (Atom summary).
  3. Filter entries by configured event codes + state FIPS.
  4. For each survivor, fetch the full CAP 1.2 XML.
  5. Filter by SAME area code overlap with configured county list.
  6. Drop expired alerts.
  7. Write active_amber_alerts.json (both text and text_core variants,
     matching the weather pipeline's shape).
  8. Compare fingerprint to the last stored one; if changed and the
     new set isn't empty, trigger amber_alert.py subprocess for the
     urgent insert. Fingerprint gate matches update_wx_alerts()'s
     'first run silent' safety guard.

Signature verification is intentionally skipped -- see AmberAlertConfig
docstring for the trust-HTTPS decision.

Idempotent: a run with the same active set writes the same JSON + same
fingerprint, and doesn't re-trigger amber_alert.py. Safe to run more
frequently than needed if we ever want to tighten latency.
"""

import fcntl
import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from ipaws import (  # noqa: E402
    alert_covers_any,
    clean_body_text,
    fetch_and_parse_cap,
    fetch_feed_entries,
    filter_feed_entries,
    is_expired,
)
from wxconfig import load_amber_alert_config, resolve_weather_data_dir  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [amber_poll] %(message)s",
)
log = logging.getLogger(__name__)


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = resolve_weather_data_dir()
ACTIVE_FILE = DATA_DIR / "active_amber_alerts.json"
FINGERPRINT_FILE = DATA_DIR / "amber_fingerprint.txt"
AMBER_ALERT_SCRIPT = str(BASE_DIR / "amber_alert.py")
LOCKFILE = "/tmp/amber-alert-poll.lock"


def _acquire_lock(path):
    f = open(path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise RuntimeError("amber_poll: another instance is running.")
    f.write(str(os.getpid()))
    f.flush()
    return f


def _safe_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _safe_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _read_prior_fingerprint():
    try:
        return Path(FINGERPRINT_FILE).read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None


def _fingerprint(active):
    """Hash the identifiers of active alerts. Any add/remove/rewording
    that changes the identifier set produces a new fingerprint. CAP
    identifiers are stable per issuance."""
    ids = sorted(entry["identifier"] for entry in active if entry.get("identifier"))
    joined = "\n".join(ids)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _statefips_prefixes_from(same_codes):
    """The Atom feed's 'statefips' category is a bare 2-digit string
    (e.g. '20' for KS). Our SAME codes are 6-digit strings (e.g.
    '020139'); positions 1:3 hold the state FIPS. Extract the unique
    set so we only fetch full CAPs for states we care about."""
    out = set()
    for c in same_codes:
        if len(c) == 6:
            out.add(c[1:3])
    return out


def _build_active_entries(cfg):
    entries = fetch_feed_entries(cfg["ipaws_base_url"])
    log.info("IPAWS feed returned %d entries", len(entries))

    statefips = _statefips_prefixes_from(cfg["same_codes"])
    candidates = filter_feed_entries(entries, cfg["event_codes"], statefips)
    log.info(
        "%d entries survived event+state prefilter (events=%s statefips=%s)",
        len(candidates), sorted(cfg["event_codes"]), sorted(statefips),
    )

    active = []
    for entry in candidates:
        cap = fetch_and_parse_cap(entry["link"])
        if cap is None:
            continue
        if is_expired(cap):
            continue
        if not alert_covers_any(cap, cfg["same_codes"]):
            continue

        description = clean_body_text(cap["description"])
        instruction = clean_body_text(cap["instruction"])
        # text  = urgent-insert version (always includes instruction).
        # text_core = forecast-append version (instruction included per
        # config -- for AMBER the instruction is usually the tip-line
        # phone number, which the operator typically wants repeated
        # every scheduled forecast).
        text_parts = [description]
        if instruction:
            text_parts.append(instruction)
        text = " ".join(p for p in text_parts if p)

        core_parts = [description]
        if instruction and cfg.get("include_instruction_in_forecast", True):
            core_parts.append(instruction)
        text_core = " ".join(p for p in core_parts if p)

        active.append({
            "identifier":  cap["identifier"],
            "event_code":  cap["event_code"] or entry["event"],
            "event":       cap["event"] or entry["event"],
            "sender_name": cap["sender_name"],
            "headline":    cap["headline"],
            "text":        text,
            "text_core":   text_core,
            "expires":     cap["expires"],
            "areas":       cap["areas"],
        })

    log.info("%d entries survived SAME-code overlap + expiry filter", len(active))
    return active


def _trigger_amber_alert():
    """Run amber_alert.py synchronously so the urgent-insert MP3 is
    delivered before this cycle considers the alert broadcast. 120s
    timeout matches wx_alert's; a few short clips + ffmpeg concat
    comfortably fit.

    check=False is intentional -- an amber_alert.py failure must not
    raise here and disrupt this cycle's own polling/retry semantics,
    but its exit status IS now inspected and logged (previously
    discarded entirely) so a real synthesis/publication failure is
    observable rather than silently invisible."""
    if not os.path.exists(AMBER_ALERT_SCRIPT):
        log.warning("Fingerprint changed but %s not found; nothing delivered.", AMBER_ALERT_SCRIPT)
        return
    try:
        result = subprocess.run([sys.executable, AMBER_ALERT_SCRIPT], check=False, timeout=120)
        if result.returncode != 0:
            log.error(
                "amber_alert.py exited %d (failure) -- AMBER audio was NOT published this cycle; "
                "the previous wx_alert.mp3 (if any) remains in place.",
                result.returncode,
            )
        else:
            log.info("Triggered amber_alert.py for updated AMBER audio.")
    except subprocess.TimeoutExpired:
        log.error("amber_alert.py timed out after 120s.")
    except Exception as e:
        log.error("Failed to trigger amber_alert.py: %s", e)


def main():
    try:
        lock = _acquire_lock(LOCKFILE)
    except RuntimeError as e:
        log.warning(str(e))
        return

    try:
        cfg = load_amber_alert_config()
        if not cfg.get("enabled"):
            log.info("AmberAlertConfig disabled; nothing to do.")
            return

        active = _build_active_entries(cfg)
        _safe_write_json(ACTIVE_FILE, active)

        new_fp = _fingerprint(active)
        prior_fp = _read_prior_fingerprint()

        if new_fp == prior_fp:
            log.info("Fingerprint unchanged; no urgent insert this cycle.")
            return

        _safe_write_text(FINGERPRINT_FILE, new_fp + "\n")

        if prior_fp is None:
            log.info("First-run fingerprint stored (no urgent insert this cycle).")
            return

        if not active:
            log.info("Alert set is now empty; fingerprint updated, no insert.")
            return

        _trigger_amber_alert()
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
