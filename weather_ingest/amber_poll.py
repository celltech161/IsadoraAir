#!/usr/bin/env python3
"""IPAWS OPEN poller for AMBER (CAE) / Blue (BLU) / Missing-Endangered
(MEP) alerts. Runs on the amber-alert-poll.timer cadence.

Pipeline:

  1. Config-gated: AmberAlertConfig.enabled=False writes a CONFIRMED-EMPTY
     active_amber_alerts.json ([]), so a previously active AMBER alert can
     never linger in the combined announcement or the forecast append.
  2. Fetch IPAWS OPEN /rest/feed (Atom summary).
  3. Filter entries by configured event codes + state FIPS.
  4. For each survivor, fetch the full CAP 1.2 XML. While an Actual alert
     of interest is held, also fetch the (bounded) entries whose summary
     LACKS an event/statefips classification -- an info-less Cancel can
     look like that (MAX_UNCLASSIFIED_CAP_FETCHES; above it the cycle keeps
     the previous snapshot).
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
    unclassified_feed_entries,
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
# Upper bound on full-CAP fetches of UNCLASSIFIED feed entries (no event
# and/or statefips classification) per cycle. They are fetched only while an
# Actual alert of interest is held (an info-less Cancel can matter only then).
# More than this cannot be examined completely, so the cycle keeps the
# previous snapshot (fail-safe; known expirations still apply) instead of
# committing a set whose cancellations were not all seen.
MAX_UNCLASSIFIED_CAP_FETCHES = 25


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


def _fetch_caps(entries):
    caps = []
    for entry in entries:
        try:
            cap = fetch_and_parse_cap(entry["link"])
        except (CapFetchError, CapParseError) as exc:
            # A partial set must never be committed as authoritative: the
            # message that could not be retrieved or interpreted may be a
            # still-active alert, or the Cancel/Update that retires one.
            raise SnapshotUnavailable(str(exc)) from exc
        caps.append((entry, cap))
    return caps


def _holds_actual_alert(caps, previous_ids):
    if previous_ids:
        return True
    return any((cap.get("status") or "").lower() == "actual"
               and (cap.get("msg_type") or "").lower() in ("alert", "update")
               for _entry, cap in caps)


def _build_active_entries(cfg, previous_ids=()):
    """previous_ids: identifiers in the previous committed snapshot (None if
    it could not be read -- then treated as possibly non-empty)."""
    entries = fetch_feed_entries(cfg["ipaws_base_url"])
    log.info("IPAWS feed returned %d entries", len(entries))

    event_codes = {c.upper() for c in cfg["event_codes"]}
    statefips = _statefips_prefixes_from(cfg["same_codes"])
    candidates = filter_feed_entries(entries, event_codes, statefips)
    log.info(
        "%d entries survived event+state prefilter (events=%s statefips=%s)",
        len(candidates), sorted(cfg["event_codes"]), sorted(statefips),
    )
    caps = _fetch_caps(candidates)

    # An authoritative Cancel may carry no <info>, so its Atom summary may lack
    # the event and/or statefips classification the prefilter relies on.
    # While we hold (or are about to hold) an Actual alert, fetch those
    # unclassified entries too -- bounded -- so such a Cancel is seen.
    unclassified = unclassified_feed_entries(entries, event_codes, statefips)
    if unclassified and _holds_actual_alert(caps, previous_ids if previous_ids is not None else ("?",)):
        log.info("%d unclassified feed entr%s examined for cancellations (limit %d)", len(unclassified),
                 "y" if len(unclassified) == 1 else "ies", MAX_UNCLASSIFIED_CAP_FETCHES)
        if len(unclassified) > MAX_UNCLASSIFIED_CAP_FETCHES:
            raise SnapshotUnavailable(
                f"{len(unclassified)} unclassified feed entries exceed the {MAX_UNCLASSIFIED_CAP_FETCHES}-fetch "
                f"limit; a cancellation among them could not be ruled out")
        caps += _fetch_caps(unclassified)

    current_ids = {id(cap) for cap in current_alerts([cap for _entry, cap in caps])}
    active = []
    for entry, cap in caps:
        if id(cap) not in current_ids:
            continue
        if (cap["event_code"] or entry["event"] or "").upper() not in event_codes:
            continue  # e.g. an unclassified entry that is an alert of another event
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


def _previous_identifiers():
    """Identifiers in the previous committed snapshot; None when it exists but
    cannot be read (callers then assume it may hold an alert)."""
    try:
        data = json.loads(Path(ACTIVE_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return tuple(e.get("identifier") for e in data if isinstance(e, dict) and e.get("identifier"))


def _refresh_snapshot():
    """Update active_amber_alerts.json for this cycle. Returns a short
    outcome string for the log."""
    cfg = load_amber_alert_config()
    if not cfg.get("enabled"):
        _safe_write_json(ACTIVE_FILE, [])
        log.info("AmberAlertConfig disabled; active AMBER set confirmed empty.")
        return "disabled"
    try:
        active = _build_active_entries(cfg, _previous_identifiers())
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
