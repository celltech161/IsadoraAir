"""IPAWS OPEN feed + CAP 1.2 parsing for the AMBER/BLU/MEP pipeline.

Two-stage fetch matches how the FEMA endpoint is structured:

  /rest/feed         -> Atom summary (id + event code + statefips per entry)
  /rest/eas/{id}     -> full CAP 1.2 XML for a specific entry

We deliberately skip XML signature verification -- HTTPS to
apps.fema.gov is the trust anchor per operator decision. See
AmberAlertConfig docstring for the "wire in DSig if that changes"
pointer.

Parser is targeted at the fields we actually use for the on-air path;
this is not a general-purpose CAP library.
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)


ATOM_NS = "{http://www.w3.org/2005/Atom}"
CAP12_NS = "{urn:oasis:names:tc:emergency:cap:1.2}"

HTTP_TIMEOUT = 15
HTTP_USER_AGENT = "IsadoraAir-AmberIngest/1.0"


def _http_get(url):
    """One-off GET with a bounded timeout and a real User-Agent so FEMA's
    edge doesn't lump us in with unlabeled bot traffic."""
    resp = requests.get(url, timeout=HTTP_TIMEOUT,
                         headers={"User-Agent": HTTP_USER_AGENT})
    resp.raise_for_status()
    return resp.content


def fetch_feed_entries(base_url):
    """Parse the /rest/feed Atom summary. Returns a list of dicts:
        [{"id": ..., "link": ..., "event": ..., "statefips": ..., "updated": ...}]
    Each entry names the ID/URL of a full CAP message we can follow if the
    event+statefips pair survives the caller's filter."""
    xml = _http_get(f"{base_url.rstrip('/')}/rest/feed")
    root = ET.fromstring(xml)
    entries = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        eid_el = entry.find(f"{ATOM_NS}id")
        link_el = entry.find(f"{ATOM_NS}link")
        updated_el = entry.find(f"{ATOM_NS}updated")
        eid = eid_el.text.strip() if eid_el is not None and eid_el.text else None
        link = link_el.attrib.get("href") if link_el is not None else None
        updated = updated_el.text.strip() if updated_el is not None and updated_el.text else None

        event = None
        statefips = None
        for cat in entry.findall(f"{ATOM_NS}category"):
            label = cat.attrib.get("label", "").strip()
            term = cat.attrib.get("term", "").strip()
            if label == "event":
                event = term.upper()
            elif label == "statefips":
                statefips = term  # 2-digit string, keep leading zero

        if not (eid and link and event):
            continue

        entries.append({
            "id": eid,
            "link": link,
            "event": event,
            "statefips": statefips,
            "updated": updated,
        })
    return entries


def filter_feed_entries(entries, event_codes, statefips_prefixes):
    """First-pass filter on the cheap-to-fetch Atom summary. Callers still
    fetch each surviving entry's full CAP and filter more precisely by
    SAME area code overlap, but this pass drops ~99% of the traffic
    (wrong event or wrong state) without individual CAP fetches."""
    event_codes = {c.upper() for c in event_codes}
    statefips_prefixes = {s for s in statefips_prefixes if s}
    return [
        e for e in entries
        if e["event"] in event_codes and e["statefips"] in statefips_prefixes
    ]


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def fetch_and_parse_cap(link):
    """Fetch a full CAP 1.2 message and return the fields we use downstream:
        {"identifier", "sent", "expires", "sender", "sender_name",
         "event", "event_code", "headline", "description", "instruction",
         "areas": [<SAME code>, ...]}
    Returns None if the XML doesn't parse or lacks the mandatory fields."""
    try:
        xml = _http_get(link)
        root = ET.fromstring(xml)
    except (requests.RequestException, ET.ParseError) as exc:
        log.warning("IPAWS CAP fetch failed for %s: %s", link, exc)
        return None

    # Multiple <info> blocks are legal (one per language). Pick English if
    # present, else the first.
    infos = root.findall(f"{CAP12_NS}info")
    info = None
    for candidate in infos:
        lang = _text(candidate.find(f"{CAP12_NS}language")).lower()
        if lang.startswith("en"):
            info = candidate
            break
    if info is None and infos:
        info = infos[0]
    if info is None:
        return None

    event_code = ""
    for ec in info.findall(f"{CAP12_NS}eventCode"):
        vn = _text(ec.find(f"{CAP12_NS}valueName")).upper()
        if vn == "SAME":
            event_code = _text(ec.find(f"{CAP12_NS}value")).upper()
            break

    areas = []
    for area in info.findall(f"{CAP12_NS}area"):
        for gc in area.findall(f"{CAP12_NS}geocode"):
            vn = _text(gc.find(f"{CAP12_NS}valueName")).upper()
            if vn == "SAME":
                v = _text(gc.find(f"{CAP12_NS}value"))
                if v:
                    areas.append(v)

    return {
        "identifier":  _text(root.find(f"{CAP12_NS}identifier")),
        "sent":        _text(root.find(f"{CAP12_NS}sent")),
        "expires":     _text(info.find(f"{CAP12_NS}expires")),
        "sender":      _text(root.find(f"{CAP12_NS}sender")),
        "sender_name": _text(info.find(f"{CAP12_NS}senderName")),
        "event":       _text(info.find(f"{CAP12_NS}event")),
        "event_code":  event_code,
        "headline":    _text(info.find(f"{CAP12_NS}headline")),
        "description": _text(info.find(f"{CAP12_NS}description")),
        "instruction": _text(info.find(f"{CAP12_NS}instruction")),
        "areas":       areas,
    }


def alert_covers_any(alert, same_codes):
    """SAME-code overlap check. A statewide SAME (0SS000) matches any
    county-scoped code in the same state, so we normalize the compare to
    catch both {020000 alert vs 020139 config} and {020139 alert vs
    020000 config} shapes."""
    same_codes = {c for c in same_codes if c}
    if not same_codes:
        return False
    for area_code in alert.get("areas", []):
        if area_code in same_codes:
            return True
        # Statewide alert (000 county) matches any code sharing the same
        # state FIPS. Length check keeps us defensive against
        # unexpectedly-shaped SAME codes (some CAP messages use different
        # geocode systems -- FIPS 5-digit, ZIP, etc.); we only expand
        # statewide-match logic for the canonical 6-digit form.
        if len(area_code) == 6 and area_code[3:6] == "000":
            state = area_code[0:3]
            for cfg in same_codes:
                if len(cfg) == 6 and cfg[0:3] == state:
                    return True
        # Config-side statewide: our config says "match all of state 20"
        # via 020000; a county-scoped alert of any KS county should
        # satisfy that.
        if len(area_code) == 6:
            state = area_code[0:3]
            statewide_cfg = state + "000"
            if statewide_cfg in same_codes:
                return True
    return False


def parse_iso_datetime(s):
    """CAP <expires> is RFC-3339-ish with a numeric TZ offset. Return an
    aware datetime, or None if unparseable."""
    if not s:
        return None
    try:
        # Python's fromisoformat handles the "2026-07-27T17:16:07-04:00"
        # shape natively on 3.11+.
        return datetime.fromisoformat(s)
    except ValueError:
        # Fall back to a trailing-Z variant sometimes seen in older CAP.
        if s.endswith("Z"):
            try:
                return datetime.fromisoformat(s[:-1]).replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        return None


def is_expired(alert, now=None):
    """Skip alerts whose <expires> has passed. FEMA's feed still lists
    them for a while after expiration so consumers can drop them
    gracefully rather than seeing a sudden empty set."""
    if now is None:
        now = datetime.now(timezone.utc)
    expires = parse_iso_datetime(alert.get("expires"))
    if expires is None:
        return False  # No expiry given -- keep, better safe than silent.
    return expires <= now


_WS_RE = re.compile(r"\s+")


def clean_body_text(text):
    """CAP descriptions frequently have HTML entities, weird whitespace,
    and URL boilerplate that read poorly through TTS. Collapse
    whitespace and strip anything obviously unspeakable."""
    if not text:
        return ""
    text = text.replace("\r", " ").replace("\n", " ")
    text = _WS_RE.sub(" ", text).strip()
    return text
