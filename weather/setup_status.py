"""Pass C (P1 1.15 / 2.4): presentation-only rendering of the Weather
Setup Status panel shown at the top of the Weather Configuration Admin
change page.

Hard rule (see the Pass C brief): `weather.diagnostics` owns facts and
states. This module ONLY organizes, labels, summarizes, links, and
presents an already-computed `WeatherDiagnosticsSnapshot` -- it must
never perform its own ORM query, filesystem check, or other readiness
inspection. Every function here is a pure function of a snapshot (or
of data already attached to one of its facts); the one call to
`get_weather_diagnostics()` happens once, in `render_setup_status()`
(or is supplied directly by a caller/test), and its result is what
every helper below actually renders.

Nothing here writes anything, contacts a network, or runs a subprocess.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone as dt_timezone

from django.urls import NoReverseMatch, reverse
from django.utils import timezone as dj_timezone
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString

from .diagnostics import DiagnosticFact, WeatherDiagnosticsSnapshot, get_weather_diagnostics

# ---------------------------------
# State presentation (labels only -- the states themselves are
# weather.diagnostics.STATES, approved and unchanged)
# ---------------------------------

STATE_LABELS = {
    "ready": "Ready",
    "degraded": "Warning",
    "needs_attention": "Needs attention",
    "optional_disabled": "Optional / Off",
    "not_applicable": "N/A",
}

# Modest, text-first color accents -- never the only signal (the label
# text above is always shown too). Chosen to stay legible on both a
# light background and Django Admin's built-in dark theme rather than
# hard-coding a light-mode-only background fill.
STATE_COLORS = {
    "ready": "#2e7d32",
    "degraded": "#b26a00",
    "needs_attention": "#c62828",
    "optional_disabled": "#666666",
    "not_applicable": "#666666",
}

OVERALL_LABELS = {
    "ready": "Ready",
    "degraded": "Ready with warnings",
    "needs_attention": "Needs attention",
}

# ---------------------------------
# Grouping (presentation metadata only -- see the Pass C brief section 3)
# ---------------------------------

_GROUP_ORDER = ["configuration", "live_data", "alert_state", "generated_audio", "other"]

_GROUP_TITLES = {
    "configuration": "Configuration",
    "live_data": "Live Weather Data",
    "alert_state": "Alert State",
    "generated_audio": "Generated Audio",
    "other": "Other diagnostics",
}

_EXACT_KEY_GROUPS = {
    "station_location": "configuration",
    "nws_config": "configuration",
    "announcer_schedule": "configuration",
    "alert_fx_cart": "configuration",
    "notifications": "configuration",
    "amber_alerts_config": "configuration",
    "weather_data_dir": "configuration",
    "forecast_cache": "live_data",
    "watch_warnings": "alert_state",
    "amber_alerts_data": "alert_state",
    "generated_artifact:wx_alert": "alert_state",
    "generated_artifact:wx_temp": "generated_audio",
    "generated_artifact:wx_obs": "generated_audio",
    "generated_artifact:wx_forecast": "generated_audio",
}


def group_for_key(key: str) -> str:
    """Which presentation group a diagnostic key belongs in. A key this
    function doesn't recognize (a future diagnostic) lands in "other"
    rather than being silently dropped -- see the Pass C brief section 3."""
    if key in _EXACT_KEY_GROUPS:
        return _EXACT_KEY_GROUPS[key]
    if key.startswith("announcer_persona:"):
        return "configuration"
    if key.startswith("weather_data_file:"):
        return "live_data"
    return "other"


# ---------------------------------
# Friendly row labels
# ---------------------------------

_FRIENDLY_LABELS = {
    "station_location": "Station Location",
    "nws_config": "NWS Lookup",
    "announcer_schedule": "Announcer Schedule",
    "alert_fx_cart": "Alert FX Cart",
    "notifications": "Failure Notifications",
    "amber_alerts_config": "AMBER-Family Alert Configuration",
    "weather_data_dir": "Weather Data Directory",
    "forecast_cache": "Forecast Cache",
    "watch_warnings": "Active Watches/Warnings",
    "amber_alerts_data": "Active AMBER-Family Alerts",
    "generated_artifact:wx_alert": "WxAlert Audio (wx_alert.mp3)",
    "generated_artifact:wx_temp": "Current Temperature Audio",
    "generated_artifact:wx_obs": "Current Conditions Audio",
    "generated_artifact:wx_forecast": "Forecast Audio",
}

# Every diagnostics.py persona summary starts with a quoted display
# label -- '"Claira" is ready (...)', '"Claira" is scheduled but ...' --
# see persona_readiness.check_persona_slots(). Pulling it out of the
# already-returned summary string (never re-querying
# WeatherVoicePersona) is exactly the "use the diagnostic summary/
# evidence" the Pass C brief asks for in section 8.
_QUOTED_LABEL_RE = re.compile(r'^"([^"]+)"')


def friendly_label(fact: DiagnosticFact) -> str:
    if fact.key in _FRIENDLY_LABELS:
        return _FRIENDLY_LABELS[fact.key]
    if fact.key.startswith("announcer_persona:"):
        slot = fact.key.split(":", 1)[1]
        match = _QUOTED_LABEL_RE.match(fact.summary or "")
        return match.group(1) if match else slot
    if fact.key.startswith("weather_data_file:"):
        return fact.key.split(":", 1)[1]
    # Unrecognized future key -- show it verbatim rather than hide it.
    return fact.key


# ---------------------------------
# Age formatting (presentation only -- never used to change `state`)
# ---------------------------------

def humanize_age(age_seconds: float | None) -> str | None:
    if age_seconds is None:
        return None
    seconds = max(0, int(age_seconds))
    if seconds < 60:
        return f"{seconds} sec ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hr ago"
    days = hours // 24
    return f"{days} day ago" if days == 1 else f"{days} days ago"


def _format_local_clock(iso_timestamp: str):
    """`generated_at` is always a UTC `...Z` ISO string (see
    diagnostics._iso()) -- converted to station/server-local time for
    display only; the canonical value is never altered."""
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except Exception:
        return iso_timestamp
    local = dj_timezone.localtime(dt.astimezone(dt_timezone.utc))
    hour12 = local.hour % 12 or 12
    period = "AM" if local.hour < 12 else "PM"
    return f"{hour12}:{local.minute:02d} {period}"


# ---------------------------------
# Overall status aggregation (presentation only -- never rewrites facts)
# ---------------------------------

def overall_state(facts) -> str:
    states = {f.state for f in facts}
    if "needs_attention" in states:
        return "needs_attention"
    if "degraded" in states:
        return "degraded"
    return "ready"


def state_counts(facts) -> dict:
    """{state: count} for every state actually present, in STATE_LABELS
    order -- never hard-coded to an expected total (persona facts and
    future diagnostics vary)."""
    counts = {state: 0 for state in STATE_LABELS}
    for fact in facts:
        counts[fact.state] = counts.get(fact.state, 0) + 1
    return counts


# ---------------------------------
# Action links (convenience only -- see Pass C brief section 7. A
# fact's correctness never depends on a link resolving; NoReverseMatch
# is swallowed and the link is simply omitted.)
# ---------------------------------

def _safe_reverse(url_name, args=None):
    try:
        return reverse(url_name, args=args or [])
    except NoReverseMatch:
        return None


def links_for_fact(fact: DiagnosticFact) -> list[tuple[str, str]]:
    links = []
    if fact.key in ("station_location", "nws_config"):
        url = _safe_reverse("admin:weather_weatherconfig_change", [1])
        if url:
            links.append(("Edit", url))
    elif fact.key == "announcer_schedule" or fact.key.startswith("announcer_persona:"):
        url = _safe_reverse("admin:weather_weathervoicepersona_changelist")
        if url:
            links.append(("Weather Voice Personas", url))
        voice_id = (fact.evidence or {}).get("tts_voice_id") if fact.key.startswith("announcer_persona:") else None
        if voice_id:
            voice_url = _safe_reverse("admin:tts_stationttsvoice_change", [voice_id])
            if voice_url:
                links.append(("Station TTS Voice", voice_url))
    elif fact.key == "alert_fx_cart":
        url = _safe_reverse("admin:library_fxcart_changelist")
        if url:
            links.append(("FX Carts", url))
    elif fact.key == "amber_alerts_config":
        url = _safe_reverse("admin:weather_amberalertconfig_changelist")
        if url:
            links.append(("AMBER Alert Configuration", url))
    elif fact.key == "weather_data_dir":
        url = _safe_reverse("admin:weather_weatherconfig_weather_env")
        if url:
            links.append(("Weather data storage", url))
    return links


# ---------------------------------
# Rendering -- format_html/format_html_join only. Diagnostic summary/
# detail/evidence text is ALWAYS passed as an ordinary (auto-escaped)
# format_html argument, never wrapped in mark_safe -- a malformed
# source file's parser-exception text can end up in `detail` and must
# render as literal text, not markup.
# ---------------------------------

_STYLE = format_html(
    "<style>"
    ".wx-setup-status{{margin:0 0 1em 0;}}"
    ".wx-setup-status .wx-overall{{font-size:1.1em;font-weight:bold;margin-bottom:.25em;}}"
    ".wx-setup-status .wx-meta{{color:#767676;font-size:.85em;margin-bottom:.75em;}}"
    ".wx-setup-status .wx-counts{{margin-bottom:1em;}}"
    ".wx-setup-status .wx-counts span{{margin-right:1em;}}"
    ".wx-setup-status table{{border-collapse:collapse;width:100%;margin-bottom:1em;}}"
    ".wx-setup-status th{{text-align:left;padding:2px 8px;border-bottom:1px solid #ccc;font-size:.85em;}}"
    ".wx-setup-status td{{padding:3px 8px;border-bottom:1px solid #eee;vertical-align:top;}}"
    ".wx-setup-status .wx-group-title{{margin:.75em 0 .25em 0;}}"
    ".wx-setup-status .wx-age{{color:#767676;font-size:.85em;white-space:nowrap;}}"
    ".wx-setup-status .wx-detail{{color:#767676;font-size:.85em;}}"
    ".wx-setup-status .wx-links a{{margin-right:.75em;}}"
    "</style>"
)


def _state_badge(fact: DiagnosticFact):
    label = STATE_LABELS.get(fact.state, fact.state)
    color = STATE_COLORS.get(fact.state, "#333333")
    return format_html('<strong style="color:{}">{}</strong>', color, label)


def _fact_row(fact: DiagnosticFact):
    label = friendly_label(fact)
    badge = _state_badge(fact)
    age = humanize_age(fact.age_seconds)
    age_html = format_html('<span class="wx-age">{}</span>', age) if age else ""
    detail_html = ""
    if fact.detail and fact.state in ("needs_attention", "degraded"):
        detail_html = format_html('<div class="wx-detail">{}</div>', fact.detail)
    link_pairs = links_for_fact(fact)
    links_html = ""
    if link_pairs:
        links_html = format_html(
            '<div class="wx-links">{}</div>',
            format_html_join("", '<a href="{}">{}</a>', ((url, text) for text, url in link_pairs)),
        )
    return format_html(
        "<tr><td>{}</td><td>{}</td><td>{}{}{}</td><td>{}</td></tr>",
        label, badge, fact.summary, detail_html, links_html, age_html,
    )


def _group_block(title, facts):
    if not facts:
        return ""
    rows = format_html_join("", "{}", ((_fact_row(f),) for f in facts))
    return format_html(
        '<h4 class="wx-group-title">{}</h4>'
        '<table><thead><tr><th>Item</th><th>State</th><th>Summary</th><th>Age</th></tr></thead>'
        "<tbody>{}</tbody></table>",
        title, rows,
    )


def render_setup_status(snapshot: WeatherDiagnosticsSnapshot = None) -> SafeString:
    """The one entry point Admin calls. `snapshot` is injectable for
    tests -- when omitted, this makes the ONE call to
    get_weather_diagnostics() for the whole panel."""
    if snapshot is None:
        snapshot = get_weather_diagnostics()

    facts = snapshot.facts
    overall = overall_state(facts)
    overall_label = OVERALL_LABELS[overall]
    overall_color = STATE_COLORS[overall]
    counts = state_counts(facts)
    counts_html = format_html_join(
        "", '<span>{} {}</span>',
        ((count, STATE_LABELS[state]) for state, count in counts.items() if count),
    )

    grouped: dict[str, list[DiagnosticFact]] = {g: [] for g in _GROUP_ORDER}
    for fact in facts:
        grouped[group_for_key(fact.key)].append(fact)

    groups_html = format_html_join(
        "", "{}",
        ((_group_block(_GROUP_TITLES[g], grouped[g]),) for g in _GROUP_ORDER),
    )

    return format_html(
        '{}<div class="wx-setup-status">'
        '<div class="wx-overall" style="color:{}">Overall: {}</div>'
        '<div class="wx-counts">{}</div>'
        '<div class="wx-meta">Status checked: {}</div>'
        "{}"
        "</div>",
        _STYLE, overall_color, overall_label, counts_html,
        _format_local_clock(snapshot.generated_at), groups_html,
    )
