#!/usr/bin/env python3
"""IPAWS OPEN poller for AMBER (CAE) / Blue (BLU) / Missing-Endangered
(MEP) alerts. Runs on the amber-alert-poll.timer cadence.

Pipeline:

  1. Config-gated: AmberAlertConfig.enabled=False writes a CONFIRMED-EMPTY
     active_amber_alerts.json ([]), so a previously active AMBER alert can
     never linger in the combined announcement or the forecast append.
  2. Fetch IPAWS OPEN /rest/feed (Atom summary).
  3. Filter entries by configured event codes + state FIPS.
  4. For each survivor, fetch the full CAP 1.2 XML.
  5. Keep only current messages: status Actual, Alert/Update (a Cancel or
     Update retires every message it references).
  6. Filter by SAME area code overlap with configured county list.
  7. Drop expired alerts.
  8. Write active_amber_alerts.json (both text and text_core variants,
     matching the weather pipeline's shape).
  9. Run the combined Weather/AMBER alert coordinator (wx_alert.py), every
     cycle -- it alone decides whether the effective spoken content changed,
     publishes ONE combined WxAlert/wx_alert.flac and nudges the engine.

A retrieval or interpretation failure (the feed, or ANY qualifying CAP
message that cannot be fetched or reliably parsed) is not a cancellation: the previous snapshot is kept untouched for that cycle, and
the coordinator still runs so authoritative <expires> times keep applying
during an outage.

Signature verification is intentionally skipped -- see AmberAlertConfig
docstring for the trust-HTTPS decision.

Idempotent: a run with the same active set writes the same JSON; the
coordinator speaks only when the effective combined content changes.
"""

import fcntl
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from ipaws import (  # noqa: E402
    CapFetchError,
    CapParseError,
    alert_covers_any,
    clean_body_text,
    current_alerts,
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
COORDINATOR_SCRIPT = str(BASE_DIR / "wx_alert.py")
# Covers the coordinator's own bounded lock wait plus one synthesis/publication.
COORDINATOR_TIMEOUT_SECONDS = 330
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


class SnapshotUnavailable(RuntimeError):
    """This cycle could not establish the complete active set (feed or CAP
    retrieval failure). The previous snapshot stays authoritative."""


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

    caps = []
    for entry in candidates:
        try:
            cap = fetch_and_parse_cap(entry["link"])
        except (CapFetchError, CapParseError) as exc:
            # A partial set must never be committed as authoritative: the
            # message that could not be retrieved or interpreted may be a
            # still-active alert, or the Cancel/Update that retires one.
            raise SnapshotUnavailable(str(exc)) from exc
        caps.append((entry, cap))

    current_ids = {id(cap) for cap in current_alerts([cap for _entry, cap in caps])}
    active = []
    for entry, cap in caps:
        if id(cap) not in current_ids:
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


def _run_alert_coordinator():
    """Run the combined Weather/AMBER coordinator (wx_alert.py) and wait for
    it. It is idempotent: unchanged effective content neither re-synthesizes
    nor queues another announcement. Its failure must not disturb this poll
    cycle; the next cycle retries."""
    if not os.path.exists(COORDINATOR_SCRIPT):
        log.warning("%s not found; combined alert state not synchronized.", COORDINATOR_SCRIPT)
        return
    try:
        result = subprocess.run([sys.executable, COORDINATOR_SCRIPT], check=False,
                                timeout=COORDINATOR_TIMEOUT_SECONDS)
        if result.returncode != 0:
            log.error("Alert coordinator (wx_alert.py) exited %d; it retries next cycle.", result.returncode)
    except subprocess.TimeoutExpired:
        log.error("Alert coordinator (wx_alert.py) timed out after %ss.", COORDINATOR_TIMEOUT_SECONDS)
    except Exception as e:
        log.error("Failed to run the alert coordinator: %s", e)


def _refresh_snapshot():
    """Update active_amber_alerts.json for this cycle. Returns a short
    outcome string for the log."""
    cfg = load_amber_alert_config()
    if not cfg.get("enabled"):
        _safe_write_json(ACTIVE_FILE, [])
        log.info("AmberAlertConfig disabled; active AMBER set confirmed empty.")
        return "disabled"
    try:
        active = _build_active_entries(cfg)
    except SnapshotUnavailable as exc:
        log.warning("AMBER retrieval incomplete (%s); previous snapshot kept.", exc)
        return "unavailable"
    except Exception as exc:  # feed transport/parse failure -- not a cancellation
        log.warning("AMBER feed retrieval failed (%s); previous snapshot kept.", exc)
        return "unavailable"
    _safe_write_json(ACTIVE_FILE, active)
    return "updated"


def main():
    try:
        lock = _acquire_lock(LOCKFILE)
    except RuntimeError as e:
        log.warning(str(e))
        return

    try:
        try:
            _refresh_snapshot()
        except Exception as exc:  # config bridge failure: snapshot untouched
            log.error("AMBER snapshot not refreshed this cycle: %s", exc)
        _run_alert_coordinator()
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
