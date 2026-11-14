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

        # An entry needs only identity and a CAP link to be kept: an
        # authoritative Cancel may legitimately carry no event/statefips
        # classification (CAP permits a Cancel without <info>). Missing
        # classifications stay None for the callers' prefilters.
        if not (eid and link):
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


def unclassified_feed_entries(entries, event_codes, statefips_prefixes):
    """Entries the classified prefilter cannot decide: a classification is
    MISSING (not merely different) and every classification present is
    compatible with ours -- e.g. an info-less Cancel whose summary carries no
    event and/or statefips. Their full CAP may be a cancellation (or update)
    of an alert we hold, so callers fetch them while they hold any Actual
    alert of interest. An entry classified with another event or another
    state is never unclassified (and never fetched)."""
    event_codes = {c.upper() for c in event_codes}
    statefips_prefixes = {s for s in statefips_prefixes if s}
    return [
        e for e in entries
        if (e["event"] is None or e["statefips"] is None)
        and (e["event"] is None or e["event"] in event_codes)
        and (e["statefips"] is None or e["statefips"] in statefips_prefixes)
    ]


def _text(el):
    return (el.text or "").strip() if el is not None else ""


class CapFetchError(RuntimeError):
    """A CAP message could not be RETRIEVED (transport/HTTP failure). Unlike an
    unparseable message, this says nothing about whether the alert is still
    active -- the caller must not treat the alert as gone."""


class CapParseError(RuntimeError):
    """A CAP message was retrieved but cannot be interpreted reliably enough
    to decide its validity or authority (unparseable XML, a missing or
    unknown mandatory field, malformed references, an Actual Alert/Update
    without <info>, an Actual Cancel without references). Like a retrieval
    failure, this is never evidence that an alert ended."""


# CAP 1.2 enumerations (OASIS CAP-v1.2-os, section 3.2; values are case-sensitive).
CAP_STATUSES = ("Actual", "Exercise", "System", "Test", "Draft")
CAP_MSG_TYPES = ("Alert", "Update", "Cancel", "Ack", "Error")
CAP_SCOPES = ("Public", "Restricted", "Private")
CAP_CATEGORIES = ("Geo", "Met", "Safety", "Security", "Rescue", "Fire", "Health", "Env", "Transport", "Infra",
                  "CBRNE", "Other")
CAP_URGENCIES = ("Immediate", "Expected", "Future", "Past", "Unknown")
CAP_SEVERITIES = ("Extreme", "Severe", "Moderate", "Minor", "Unknown")
CAP_CERTAINTIES = ("Observed", "Likely", "Possible", "Unlikely", "Unknown")
_CAP_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[-+]\d{2}:\d{2}$")
_CAP_RESTRICTED = re.compile(r"[\s,<&]")


def _require_cap_datetime(value, what):
    """CAP 1.2 dateTime: YYYY-MM-DDThh:mm:ss with an explicit numeric offset."""
    if not _CAP_DATETIME.match(value) or parse_iso_datetime(value) is None:
        raise CapParseError(f"{what} is not a CAP dateTime: {value!r}")


def _validate_info(info, label):
    """Mandatory <info> elements (CAP 1.2 section 3.2.2): one or more category,
    event, urgency, severity, certainty -- present, non-empty and, for the
    enumerated ones, a defined value. Optional elements (headline,
    description, instruction, expires, area...) are not required; an
    expires that IS present must be a valid dateTime."""
    categories = [_text(c) for c in info.findall(f"{CAP12_NS}category")]
    if not categories or any(c not in CAP_CATEGORIES for c in categories):
        raise CapParseError(f"{label} <info> has a missing or invalid category: {categories!r}")
    if not _text(info.find(f"{CAP12_NS}event")):
        raise CapParseError(f"{label} <info> has no event")
    for name, allowed in (("urgency", CAP_URGENCIES), ("severity", CAP_SEVERITIES),
                          ("certainty", CAP_CERTAINTIES)):
        value = _text(info.find(f"{CAP12_NS}{name}"))
        if value not in allowed:
            raise CapParseError(f"{label} <info> has a missing or invalid {name}: {value!r}")
    expires = _text(info.find(f"{CAP12_NS}expires"))
    if expires:
        _require_cap_datetime(expires, f"{label} <info> expires")


def fetch_and_parse_cap(link):
    """Fetch a full CAP 1.2 message and return the fields we use downstream:
        {"identifier", "sent", "expires", "sender", "sender_name",
         "status", "msg_type", "references": [<identifier>, ...],
         "event", "event_code", "headline", "description", "instruction",
         "areas": [<SAME code>, ...]}
    Raises CapFetchError when the message could not be retrieved and
    CapParseError when it cannot be interpreted reliably.

    Validation (CAP 1.2, focused -- no schema framework):
      * root (always): identifier, sender, sent, status, msgType, scope present
        and non-empty; identifier/sender without whitespace, commas or "<"/"&";
        sent a dateTime; status/msgType/scope defined values; <references>
        well-formed sender,identifier,sent triplets.
      * a non-Actual message has no authority over Actual alerts, so its <info>
        is not judged: it is returned for completeness and ignored downstream.
      * an Actual Alert/Update must carry an <info> block whose mandatory
        elements (category, event, urgency, severity, certainty) are present
        and valid -- an incomplete one (e.g. <info/>) can never supersede the
        issuance it references;
      * an Actual Cancel may have no <info> (CAP permits it; it is parsed from
        its root fields) but must reference what it cancels; an <info> it does
        carry must be valid."""
    try:
        xml = _http_get(link)
    except requests.RequestException as exc:
        raise CapFetchError(f"IPAWS CAP fetch failed for {link}: {exc}") from exc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise CapParseError(f"IPAWS CAP message unparseable for {link}: {exc}") from exc

    fields = {name: _text(root.find(f"{CAP12_NS}{name}"))
              for name in ("identifier", "sender", "sent", "status", "msgType", "scope")}
    missing = [name for name, value in fields.items() if not value]
    if missing:
        raise CapParseError(f"IPAWS CAP message {link} lacks mandatory {missing}")
    label = f"IPAWS CAP {fields['msgType']} {fields['identifier']} ({link})"
    for name in ("identifier", "sender"):
        if _CAP_RESTRICTED.search(fields[name]):
            raise CapParseError(f"{label}: {name} contains whitespace, a comma or a restricted character")
    _require_cap_datetime(fields["sent"], f"{label} sent")
    for name, allowed in (("status", CAP_STATUSES), ("msgType", CAP_MSG_TYPES), ("scope", CAP_SCOPES)):
        if fields[name] not in allowed:
            raise CapParseError(f"{label} has an undefined {name} {fields[name]!r}")
    status, msg_type = fields["status"].lower(), fields["msgType"].lower()
    references_text = _text(root.find(f"{CAP12_NS}references"))
    try:
        references = parse_references(references_text)
    except ValueError as exc:
        raise CapParseError(f"{label} has malformed references: {exc}") from exc

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

    if status == "actual":
        if msg_type in ("alert", "update") and info is None:
            raise CapParseError(f"{label} has no <info> to evaluate")
        if msg_type == "cancel" and not references:
            raise CapParseError(f"{label} references nothing to cancel")
        if info is not None:
            _validate_info(info, label)

    event_code = ""
    areas = []
    if info is not None:
        for ec in info.findall(f"{CAP12_NS}eventCode"):
            vn = _text(ec.find(f"{CAP12_NS}valueName")).upper()
            if vn == "SAME":
                event_code = _text(ec.find(f"{CAP12_NS}value")).upper()
                break
        for area in info.findall(f"{CAP12_NS}area"):
            for gc in area.findall(f"{CAP12_NS}geocode"):
                vn = _text(gc.find(f"{CAP12_NS}valueName")).upper()
                if vn == "SAME":
                    v = _text(gc.find(f"{CAP12_NS}value"))
                    if v:
                        areas.append(v)

    def info_text(name):
        return _text(info.find(f"{CAP12_NS}{name}")) if info is not None else ""

    return {
        "identifier":  fields["identifier"],
        "sent":        fields["sent"],
        "status":      fields["status"],
        "msg_type":    fields["msgType"],
        "references":  references,
        "expires":     info_text("expires"),
        "sender":      fields["sender"],
        "sender_name": info_text("senderName"),
        "event":       info_text("event"),
        "event_code":  event_code,
        "headline":    info_text("headline"),
        "description": info_text("description"),
        "instruction": info_text("instruction"),
        "areas":       areas,
    }


def parse_references(text):
    """CAP <references>: whitespace-separated "sender,identifier,sent" triplets.
    Returns the referenced identifiers (an Update/Cancel supersedes them).
    Raises ValueError for a triplet that is not exactly sender,identifier,sent."""
    out = []
    for triplet in (text or "").split():
        parts = triplet.split(",")
        if len(parts) != 3 or not all(parts):
            raise ValueError(f"not a sender,identifier,sent triplet: {triplet!r}")
        out.append(parts[1])
    return out


def current_alerts(caps):
    """Reduce parsed CAP messages to the Actual alerts still in effect.

    Only status Actual messages have authority: a Test/Exercise/System/Draft
    message never airs AND never cancels or supersedes anything. Among Actual
    messages, a Cancel retires every message it references and never airs;
    an Update supersedes every message it references (chains included, in
    any feed order); Ack/Error never air. Order of survivors is preserved."""
    actual = [cap for cap in caps if (cap.get("status") or "").lower() == "actual"]
    superseded = set()
    for cap in actual:
        if (cap.get("msg_type") or "").lower() in ("update", "cancel"):
            superseded.update(cap.get("references") or [])
    return [
        cap for cap in actual
        if (cap.get("msg_type") or "").lower() in ("alert", "update")
        and cap.get("identifier") not in superseded
    ]


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
