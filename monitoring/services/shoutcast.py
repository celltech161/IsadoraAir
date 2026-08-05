"""Shared Shoutcast v2 /statistics fetcher -- extracted from
monitor.py's MonitorManager._fetch_shoutcast_stats (which now delegates
here) so the new `encoder_group` health probe (probes.py) can check the
real, external, per-SID connection state without a second HTTP/XML
parser. Two callers, one wire-format understanding.

Wire format observed live on Shoutcast v2.6.1 (confirmed during the
2026-08-05 encoder outage -- this is what actually proved the station
was off the air, independent of anything this project's own monitoring
was reporting):

    <SHOUTCASTSERVER>
      <STREAMSTATS>
        <TOTALSTREAMS>4</TOTALSTREAMS>       <-- server-wide totals
        ...
        <STREAM id="1">                      <-- STREAM lives INSIDE
          <CURRENTLISTENERS>4</CURRENTLISTENERS>  STREAMSTATS, not as
          <STREAMSTATUS>1</STREAMSTATUS>          a root child.
          ...                                <-- 1 = active, 0 = down
        </STREAM>
        <STREAM id="2">...</STREAM>
      </STREAMSTATS>
    </SHOUTCASTSERVER>

`.//STREAM` descends into any location -- robust to any future SC
version that reorganizes the wrapping element."""
import urllib.request
import xml.etree.ElementTree as ET

STATS_TIMEOUT_SECONDS = 3.0  # LAN localhost/same-subnet; anything above ~1s is a real problem


def fetch_shoutcast_stats(host, port, timeout=STATS_TIMEOUT_SECONDS):
    """GET /statistics from a Shoutcast v2 server, return
    {sid: {"up": bool, "listeners": int}} keyed by string SID (matching
    Encoder.shoutcast_sid's own string type). Returns {} on any
    transport or parse failure -- every caller treats a missing SID
    entry as "down", which is also the correct behavior for "server
    unreachable" (every configured SID ends up absent, not a false
    "up")."""
    url = f"http://{host}:{port}/statistics"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read()
    except Exception:
        return {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return {}
    out = {}
    for stream in root.findall(".//STREAM"):
        sid = stream.get("id", "").strip()
        if not sid:
            continue
        listeners_el = stream.find("CURRENTLISTENERS")
        status_el = stream.find("STREAMSTATUS")
        listeners = int((listeners_el.text or "0").strip()) if listeners_el is not None else 0
        up = (status_el is not None and (status_el.text or "").strip() == "1")
        out[sid] = {"up": up, "listeners": listeners}
    return out
