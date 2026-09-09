"""Weather diagnostics/readiness authority (P1 1.15 / 2.4 Pass B).

One reusable, side-effect-free, structured snapshot of Weather
configuration/data/artifact evidence -- consumed by the Admin "Weather
data storage" subpage and by the `weather_diagnostics` management
command (Pass B's two proving consumers). Later work (Setup Status UI,
Monitoring, provenance/freshness policy) is expected to build on this
same authority rather than re-deriving its own notion of "is Weather
healthy".

Design rule (see the Pass B brief): evidence first, policy later. This
module reports facts -- a file exists, a timestamp's age in seconds, a
persona has no voice, a schedule doesn't cover every hour -- it does
NOT invent new staleness/fallback thresholds. The one deliberate
exception is wx_forecast.py's own pre-existing 6-hour "cache is stale"
warning, which is exposed here as `degraded` evidence rather than
reinvented.

State vocabulary (`DiagnosticFact.state`):
    ready             -- evidence collected and everything checked is fine
    optional_disabled -- a feature is intentionally off; absence of its
                          evidence is therefore expected, not a failure
    degraded          -- evidence exists but is suboptimal (a forecast
                          cache older than the existing 6h warning, a
                          recurring artifact not yet produced) -- doesn't
                          necessarily require operator action
    needs_attention   -- something requires operator action (missing
                          required config, malformed data, a scheduled
                          persona with no enabled voice, a delivered
                          file with no matching Track)
    not_applicable    -- deliberate 5th state, added in Pass B: evidence
                          for an EVENT-DRIVEN artifact (WxAlert/
                          wx_alert.mp3, the urgent spoken watch/warning
                          and AMBER-family statement -- NOT the FX
                          "alert beep", which is a separate, always-
                          available cart tracked by `alert_fx_cart`)
                          when there is currently no active alert. The
                          brief is explicit that "event-driven alert
                          audio must not automatically be considered
                          broken merely because there is currently no
                          active alert" -- none of the other four states
                          honestly describes "nothing to check right
                          now", so this is a narrow, named addition
                          rather than overloading `ready` (which would
                          claim more than we know) or `optional_disabled`
                          (which implies an operator turned something off).

This module performs NO network requests, NO subprocess/TTS/ffmpeg
calls, and NO writes -- see test_diagnostics.py's own
DiagnosticsSideEffectTests for the enforced proof. It uses ordinary
read-only Django ORM queries and read-only filesystem access only.
r0051 correctness fix: `WeatherConfig.load()` / `AmberAlertConfig.
load()` use get_or_create and may silently persist a new singleton
(and, for WeatherConfig, a default WeatherVoicePersona) merely by being
called -- diagnostics must never do that just to inspect a
configuration that doesn't exist yet, so this module reads both
singletons with a plain `.filter(pk=1).first()` and represents an
absent row explicitly (see `_check_station_location` et al. and
`_check_amber_config`/`_check_amber_alerts_data`) rather than ever
calling `.load()` itself. Ordinary Admin workflows may keep using
`.load()` where creating the row on first visit is the intended
behavior; only this collector must stay pure.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Any

from django.conf import settings
from django.utils import timezone as dj_timezone

from library.models import Category, Track
from .models import AmberAlertConfig, WeatherConfig, WeatherVoicePersona, normalize_alert_sound_trigger_events
from .persona_readiness import check_persona_slots
from .voice_schedule import ScheduleError, expand_to_hours

STATES = ("ready", "optional_disabled", "degraded", "needs_attention", "not_applicable")

# The pre-existing production warning boundary from weather_ingest/wx_forecast.py
# (get_periods()'s own `if age_hr > 6:` check) -- exposed here, not reinvented.
FORECAST_CACHE_WARN_HOURS = 6

# Ordinary (non-forecast, non-alert) weather data files with a plain
# JSON-object shape and no semantic timestamp field of their own beyond
# what's noted per-file below.
_SIMPLE_DATA_FILES = ["processed_weather.json", "sky_condition.json"]

GENERATED_ARTIFACTS = [
    # (fact key suffix, category code, filename, event_driven)
    ("wx_temp", "WxTemp", "current_temp.mp3", False),
    ("wx_obs", "WxObs", "current_obs.mp3", False),
    ("wx_forecast", "WxForecast", "forecast.mp3", False),
    ("wx_alert", "WxAlert", "wx_alert.mp3", True),
]

_NWS_ZONE_RE = re.compile(r"^[A-Z]{2}[CZ]\d{3}$")


@dataclass(frozen=True)
class DiagnosticFact:
    """One structured diagnostic fact. Raw evidence (path/timestamp/
    age_seconds/count/evidence) is kept separate from the interpreted
    `state` so future freshness policy can change without rewriting how
    the underlying evidence is collected (see module docstring)."""

    key: str
    state: str
    summary: str
    detail: str = ""
    path: str | None = None
    timestamp: str | None = None
    age_seconds: float | None = None
    count: int | None = None
    evidence: dict[str, Any] | None = None

    def __post_init__(self):
        if self.state not in STATES:
            raise ValueError(f"invalid diagnostic state {self.state!r} for {self.key!r}")

    def to_dict(self) -> dict[str, Any]:
        d = {
            "key": self.key,
            "state": self.state,
            "summary": self.summary,
            "detail": self.detail,
        }
        for attr in ("path", "timestamp", "age_seconds", "count", "evidence"):
            value = getattr(self, attr)
            if value is not None:
                d[attr] = value
        return d


@dataclass(frozen=True)
class WeatherDiagnosticsSnapshot:
    generated_at: str
    facts: tuple[DiagnosticFact, ...]

    def get(self, key: str) -> DiagnosticFact | None:
        for fact in self.facts:
            if fact.key == key:
                return fact
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "facts": [f.to_dict() for f in self.facts],
        }

    @property
    def needs_attention(self) -> tuple[DiagnosticFact, ...]:
        return tuple(f for f in self.facts if f.state == "needs_attention")


def _iso(dt: datetime) -> str:
    return dt.astimezone(dt_timezone.utc).isoformat().replace("+00:00", "Z")


def _age_seconds(now: datetime, dt: datetime) -> float:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    return max(0.0, (now - dt).total_seconds())


def _parse_semantic_timestamp(value: str) -> datetime | None:
    """Best-effort ISO-8601 parse (the shape every producer in this
    pipeline actually writes -- record_gateway_payload's `Z`-suffixed
    UTC, smooth_wind()'s same, wind-history entries' offset-aware
    isoformat). Returns None (never raises) for anything else, so
    callers can fall back to mtime and label it explicitly as such."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def get_weather_diagnostics(now=None, data_dir=None, library_root=None) -> WeatherDiagnosticsSnapshot:
    """The one public entry point. All dependencies are injectable for
    deterministic tests; every argument defaults to the real runtime
    value. Never raises for ordinary "missing/malformed data" cases --
    those become `needs_attention`/`degraded` facts instead."""
    now = now or dj_timezone.now()
    data_dir = Path(data_dir) if data_dir is not None else Path(
        getattr(settings, "WEATHER_DATA_DIR", "/var/lib/isadoraair/weather")
    )
    library_root = Path(library_root) if library_root is not None else Path(
        getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music")
    )

    facts: list[DiagnosticFact] = []
    # Plain read-only lookup -- NEVER WeatherConfig.load()/AmberAlertConfig.
    # load(), which are get_or_create and would silently persist a new
    # singleton (WeatherConfig.load() a WeatherVoicePersona too) just
    # because diagnostics asked. `cfg`/`amber_cfg` are None, not a
    # freshly-created row, when nothing has been configured yet -- see
    # module docstring and every _check_*(cfg) function below.
    cfg = WeatherConfig.objects.filter(pk=1).first()
    amber_cfg = AmberAlertConfig.objects.filter(pk=1).first()

    facts.append(_check_station_location(cfg))
    facts.append(_check_nws_config(cfg))
    facts.extend(_check_announcer_schedule(cfg))
    facts.append(_check_alert_fx_cart(cfg))
    facts.append(_check_notifications(cfg))
    facts.append(_check_amber_config(amber_cfg))

    facts.append(_check_data_dir(data_dir))
    for filename in _SIMPLE_DATA_FILES:
        facts.append(_check_simple_json_file(data_dir / filename, now))
    facts.append(_check_latest_weather(data_dir / "latest_weather.json", now))
    facts.append(_check_wind_history(data_dir / "wind_history.json", now))
    facts.append(_check_smoothed_wind(data_dir / "smoothed_wind.json", now))
    facts.append(_check_forecast_cache(data_dir / "wx_forecast_cache.json", now))
    watch_fact = _check_watch_warnings(data_dir / "active_watches_warnings.json", now)
    facts.append(watch_fact)
    amber_data_fact = _check_amber_alerts_data(amber_cfg, data_dir / "active_amber_alerts.json", now)
    facts.append(amber_data_fact)

    # WxAlert/wx_alert.mp3 is shared by both source types and, being
    # event-driven, needs to know whether an alert is CURRENTLY active
    # before file/Track presence can be interpreted at all -- see
    # _classify_alert_applicability() and _check_generated_artifact().
    alert_applicability = _classify_alert_applicability(watch_fact, amber_data_fact)
    for suffix, category_code, filename, event_driven in GENERATED_ARTIFACTS:
        applicability = alert_applicability if event_driven else None
        facts.append(_check_generated_artifact(
            suffix, category_code, filename, event_driven, library_root, now, applicability,
        ))

    return WeatherDiagnosticsSnapshot(generated_at=_iso(now), facts=tuple(facts))


# ---------------------------------
# Configuration readiness
# ---------------------------------

# Shared wording for every config-dependent fact when no WeatherConfig
# row exists at all yet -- distinct from "a row exists but this
# particular field is invalid". See get_weather_diagnostics()'s own
# read-only lookup and the module docstring.
_NO_WEATHER_CONFIG_SUMMARY = "Weather Configuration has not been created yet -- visit Weather Configuration in Admin to initialize it."


def _check_station_location(cfg: WeatherConfig | None) -> DiagnosticFact:
    if cfg is None:
        return DiagnosticFact(key="station_location", state="needs_attention", summary=_NO_WEATHER_CONFIG_SUMMARY)
    problems = []
    if not (-90.0 <= cfg.station_lat <= 90.0):
        problems.append(f"station_lat {cfg.station_lat!r} is outside -90..90.")
    if not (-180.0 <= cfg.station_lon <= 180.0):
        problems.append(f"station_lon {cfg.station_lon!r} is outside -180..180.")
    evidence = {"station_lat": cfg.station_lat, "station_lon": cfg.station_lon,
                "sun_alt_threshold_deg": cfg.sun_alt_threshold_deg}
    if problems:
        return DiagnosticFact(
            key="station_location", state="needs_attention",
            summary="Station location has an out-of-range value.",
            detail=" ".join(problems), evidence=evidence,
        )
    return DiagnosticFact(
        key="station_location", state="ready",
        summary=f"Station location set ({cfg.station_lat}, {cfg.station_lon}).",
        evidence=evidence,
    )


def _check_nws_config(cfg: WeatherConfig | None) -> DiagnosticFact:
    """Structural sanity only -- no network call to api.weather.gov."""
    if cfg is None:
        return DiagnosticFact(key="nws_config", state="needs_attention", summary=_NO_WEATHER_CONFIG_SUMMARY)
    problems = []
    if not _NWS_ZONE_RE.match(cfg.nws_alert_zone or ""):
        problems.append(f"nws_alert_zone {cfg.nws_alert_zone!r} does not look like NWS zone/county format (e.g. KSC143).")
    if not (cfg.nws_forecast_office or "").strip():
        problems.append("nws_forecast_office is blank.")
    if cfg.nws_forecast_grid_x is None or cfg.nws_forecast_grid_y is None:
        problems.append("forecast grid X/Y is not set.")
    stations = [s.strip() for s in (cfg.nws_cloud_stations or "").split(",") if s.strip()]
    if not stations:
        problems.append("nws_cloud_stations has no METAR station codes configured.")
    evidence = {
        "nws_alert_zone": cfg.nws_alert_zone,
        "nws_forecast_office": cfg.nws_forecast_office,
        "nws_forecast_grid_x": cfg.nws_forecast_grid_x,
        "nws_forecast_grid_y": cfg.nws_forecast_grid_y,
        "nws_cloud_stations": stations,
    }
    if problems:
        return DiagnosticFact(
            key="nws_config", state="needs_attention",
            summary="NWS lookup configuration is incomplete or malformed.",
            detail=" ".join(problems), evidence=evidence,
        )
    return DiagnosticFact(
        key="nws_config", state="ready",
        summary=f"NWS lookup configured ({cfg.nws_forecast_office} {cfg.nws_forecast_grid_x},{cfg.nws_forecast_grid_y}, zone {cfg.nws_alert_zone}).",
        evidence=evidence,
    )


def _check_announcer_schedule(cfg: WeatherConfig | None) -> list[DiagnosticFact]:
    """Reuses voice_schedule.expand_to_hours() (schedule shape) and
    persona_readiness.check_persona_slots() (persona/voice readiness)
    -- the SAME authorities WeatherConfigForm's own validation uses, so
    this can never disagree with what the admin form would accept."""
    if cfg is None:
        return [DiagnosticFact(key="announcer_schedule", state="needs_attention", summary=_NO_WEATHER_CONFIG_SUMMARY)]
    facts = []
    try:
        hour_to_slot = expand_to_hours(cfg.voice_schedule)
    except ScheduleError as exc:
        facts.append(DiagnosticFact(
            key="announcer_schedule", state="needs_attention",
            summary="Announcer schedule is malformed or incomplete.",
            detail=str(exc), evidence={"voice_schedule": cfg.voice_schedule},
        ))
        return facts

    referenced_slots = sorted(set(hour_to_slot.values()))
    facts.append(DiagnosticFact(
        key="announcer_schedule", state="ready",
        summary=f"Schedule covers all 24 hours across {len(referenced_slots)} persona slot(s).",
        evidence={"referenced_slots": referenced_slots},
    ))
    for check in check_persona_slots(referenced_slots):
        if check.problem:
            facts.append(DiagnosticFact(
                key=f"announcer_persona:{check.slot}", state="needs_attention",
                summary=check.problem, evidence={
                    "slot": check.slot, "exists": check.exists,
                    "tts_voice_id": check.tts_voice_id,
                },
            ))
        else:
            facts.append(DiagnosticFact(
                key=f"announcer_persona:{check.slot}", state="ready",
                summary=f'"{check.label}" is ready ({check.tts_voice_name}).',
                evidence={
                    "slot": check.slot, "tts_voice_id": check.tts_voice_id,
                    "tts_voice_name": check.tts_voice_name,
                },
            ))
    return facts


def _check_alert_fx_cart(cfg: WeatherConfig | None) -> DiagnosticFact:
    """r0053 amendment: also owns readiness of WeatherConfig.
    alert_sound_trigger_events (the Weather Alert Beep's qualifying-
    event list) -- weather.diagnostics stays the one readiness
    authority for this, never a second Admin-only interpretation. This
    fact is about the repeating sonar/ping FX Cart ONLY; it never
    reflects on generated_artifact:wx_alert (the spoken urgent-alert
    artifact), which has its own independent applicability logic."""
    if cfg is None:
        return DiagnosticFact(key="alert_fx_cart", state="needs_attention", summary=_NO_WEATHER_CONFIG_SUMMARY)
    if not cfg.alert_sound_enabled:
        return DiagnosticFact(
            key="alert_fx_cart", state="optional_disabled",
            summary="Alert beep is disabled.",
        )

    # A stored NULL is a deliberate migration-compatibility state (see
    # normalize_alert_sound_trigger_events()), never malformed data and
    # never an operator's explicit empty choice -- it evaluates to the
    # same four legacy defaults an upgraded row has always effectively
    # had, so it proceeds straight into the normal readiness checks
    # below exactly as if those defaults had been saved explicitly.
    triggers = normalize_alert_sound_trigger_events(cfg.alert_sound_trigger_events)
    well_formed = isinstance(triggers, list) and all(isinstance(t, str) and t.strip() for t in triggers)
    if not well_formed:
        return DiagnosticFact(
            key="alert_fx_cart", state="needs_attention",
            summary="Weather Alert Beep trigger-event configuration is malformed.",
            detail="alert_sound_trigger_events must be a list of non-empty NWS event-name strings.",
            evidence={"trigger_events": triggers},
        )
    if not triggers:
        return DiagnosticFact(
            key="alert_fx_cart", state="optional_disabled",
            summary="Weather Alert Beep has no trigger event types configured; it will not fire.",
            evidence={"trigger_event_count": 0},
        )

    cart = cfg.alert_sound_cart
    evidence = {"trigger_events": triggers, "trigger_event_count": len(triggers)}
    if cart is None:
        return DiagnosticFact(
            key="alert_fx_cart", state="needs_attention",
            summary="Alert beep is enabled but no FX Cart is selected.",
            evidence=evidence,
        )
    if not Path(cart.filepath).is_file():
        return DiagnosticFact(
            key="alert_fx_cart", state="needs_attention",
            summary=f'Alert FX Cart "{cart.name}" is selected but its audio file is missing.',
            path=cart.filepath, evidence={**evidence, "cart_id": cart.id, "cart_name": cart.name},
        )
    return DiagnosticFact(
        key="alert_fx_cart", state="ready",
        summary=f'Alert beep configured with FX Cart "{cart.name}" for {len(triggers)} trigger event type(s).',
        path=cart.filepath, evidence={**evidence, "cart_id": cart.id, "cart_name": cart.name},
    )


def _check_notifications(cfg: WeatherConfig | None) -> DiagnosticFact:
    if cfg is None:
        return DiagnosticFact(key="notifications", state="needs_attention", summary=_NO_WEATHER_CONFIG_SUMMARY)
    if not cfg.notify_email:
        return DiagnosticFact(
            key="notifications", state="optional_disabled",
            summary="Failure-notification email is not configured.",
        )
    return DiagnosticFact(
        key="notifications", state="ready",
        summary=f"Failure notifications will be sent to {cfg.notify_email}.",
        evidence={"notify_email": cfg.notify_email},
    )


def _check_amber_config(cfg: AmberAlertConfig | None) -> DiagnosticFact:
    # No row at all is equivalent to the model's own enabled=False
    # default -- reported as optional_disabled, same as an explicit
    # off, but worded so it's never mistaken for "checked and enabled".
    if cfg is None:
        return DiagnosticFact(
            key="amber_alerts_config", state="optional_disabled",
            summary="AMBER-family alerts have not been configured (defaults to disabled).",
        )
    if not cfg.enabled:
        return DiagnosticFact(
            key="amber_alerts_config", state="optional_disabled",
            summary="AMBER-family alerts are disabled.",
        )
    problems = []
    if not cfg.event_codes_set:
        problems.append("no event codes configured.")
    if not cfg.same_codes_set:
        problems.append("no SAME area codes configured.")
    if not (cfg.ipaws_base_url or "").strip():
        problems.append("ipaws_base_url is blank.")
    evidence = {
        "event_codes": sorted(cfg.event_codes_set),
        "same_codes": sorted(cfg.same_codes_set),
        "poll_cadence_minutes": cfg.poll_cadence_minutes,
    }
    if problems:
        return DiagnosticFact(
            key="amber_alerts_config", state="needs_attention",
            summary="AMBER-family alerts are enabled but misconfigured.",
            detail=" ".join(problems), evidence=evidence,
        )
    return DiagnosticFact(
        key="amber_alerts_config", state="ready",
        summary=f"AMBER-family alerts enabled ({len(cfg.event_codes_set)} event code(s), "
                f"{len(cfg.same_codes_set)} area code(s)).",
        evidence=evidence,
    )


# ---------------------------------
# Weather data directory
# ---------------------------------

def _check_data_dir(data_dir: Path) -> DiagnosticFact:
    if not data_dir.exists():
        return DiagnosticFact(
            key="weather_data_dir", state="needs_attention",
            summary="Weather data directory does not exist.", path=str(data_dir),
        )
    if not data_dir.is_dir():
        return DiagnosticFact(
            key="weather_data_dir", state="needs_attention",
            summary="Weather data path exists but is not a directory.", path=str(data_dir),
        )
    try:
        next(data_dir.iterdir(), None)
    except PermissionError:
        return DiagnosticFact(
            key="weather_data_dir", state="needs_attention",
            summary="Weather data directory exists but is not readable.", path=str(data_dir),
        )
    return DiagnosticFact(
        key="weather_data_dir", state="ready",
        summary="Weather data directory exists and is readable.", path=str(data_dir),
    )


def _load_json_file(path: Path):
    """Returns (data, error). error is None on success; a short string
    describing why parsing failed otherwise. Never raises."""
    if not path.is_file():
        return None, "missing"
    try:
        return json.loads(path.read_text()), None
    except Exception as exc:
        return None, f"malformed JSON: {exc}"


def _mtime_evidence(path: Path, now):
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=dt_timezone.utc)
    except OSError:
        return None, None
    return _iso(mtime), _age_seconds(now, mtime)


def _check_simple_json_file(path: Path, now) -> DiagnosticFact:
    """Generic evidence for a data file with no semantic timestamp and
    no established staleness policy -- presence/parse-validity plus
    file mtime ONLY, explicitly labelled as mtime (never claimed to be
    "last successful fetch"). No age-based state judgement is made
    here; see module docstring on not inventing new staleness policy.

    `now` is always the snapshot's own resolved clock (see
    get_weather_diagnostics()) -- never resolved independently here, so
    an injected `now=` stays authoritative for every fact, not just
    most of them."""
    key = f"weather_data_file:{path.name}"
    data, error = _load_json_file(path)
    if error == "missing":
        return DiagnosticFact(
            key=key, state="degraded",
            summary=f"{path.name} does not exist yet.", path=str(path),
            evidence={"exists": False},
        )
    if error:
        return DiagnosticFact(
            key=key, state="needs_attention",
            summary=f"{path.name} exists but failed to parse.", detail=error, path=str(path),
            evidence={"exists": True},
        )
    timestamp, age = _mtime_evidence(path, now)
    return DiagnosticFact(
        key=key, state="ready",
        summary=f"{path.name} exists and parses.", path=str(path),
        timestamp=timestamp, age_seconds=age,
        evidence={"timestamp_source": "file_mtime", "exists": True},
    )


def _check_latest_weather(path: Path, now) -> DiagnosticFact:
    key = "weather_data_file:latest_weather.json"
    data, error = _load_json_file(path)
    if error == "missing":
        return DiagnosticFact(key=key, state="degraded", summary="latest_weather.json does not exist yet.", path=str(path), evidence={"exists": False})
    if error:
        return DiagnosticFact(key=key, state="needs_attention", summary="latest_weather.json exists but failed to parse.", detail=error, path=str(path), evidence={"exists": True})
    ts = _parse_semantic_timestamp(data.get("timestamp")) if isinstance(data, dict) else None
    if ts is not None:
        return DiagnosticFact(
            key=key, state="ready", summary="latest_weather.json exists and parses.",
            path=str(path), timestamp=_iso(ts), age_seconds=_age_seconds(now, ts),
            evidence={"timestamp_source": "payload.timestamp", "exists": True},
        )
    timestamp, age = _mtime_evidence(path, now)
    return DiagnosticFact(
        key=key, state="ready", summary="latest_weather.json exists and parses (no semantic timestamp found).",
        path=str(path), timestamp=timestamp, age_seconds=age,
        evidence={"timestamp_source": "file_mtime", "exists": True},
    )


def _check_wind_history(path: Path, now) -> DiagnosticFact:
    key = "weather_data_file:wind_history.json"
    data, error = _load_json_file(path)
    if error == "missing":
        return DiagnosticFact(key=key, state="degraded", summary="wind_history.json does not exist yet.", path=str(path), evidence={"exists": False})
    if error:
        return DiagnosticFact(key=key, state="needs_attention", summary="wind_history.json exists but failed to parse.", detail=error, path=str(path), evidence={"exists": True})
    if not isinstance(data, list):
        return DiagnosticFact(key=key, state="needs_attention", summary="wind_history.json is not a list.", path=str(path), evidence={"exists": True})
    latest_ts = None
    for entry in data:
        if isinstance(entry, dict):
            ts = _parse_semantic_timestamp(entry.get("time"))
            if ts is not None and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
    evidence = {"entry_count": len(data), "exists": True}
    if latest_ts is not None:
        return DiagnosticFact(
            key=key, state="ready", summary=f"wind_history.json has {len(data)} entr(y/ies).",
            path=str(path), count=len(data), timestamp=_iso(latest_ts), age_seconds=_age_seconds(now, latest_ts),
            evidence={**evidence, "timestamp_source": "latest_entry.time"},
        )
    timestamp, age = _mtime_evidence(path, now)
    return DiagnosticFact(
        key=key, state="ready", summary=f"wind_history.json has {len(data)} entr(y/ies).",
        path=str(path), count=len(data), timestamp=timestamp, age_seconds=age,
        evidence={**evidence, "timestamp_source": "file_mtime"},
    )


def _check_smoothed_wind(path: Path, now) -> DiagnosticFact:
    key = "weather_data_file:smoothed_wind.json"
    data, error = _load_json_file(path)
    if error == "missing":
        return DiagnosticFact(key=key, state="degraded", summary="smoothed_wind.json does not exist yet.", path=str(path), evidence={"exists": False})
    if error:
        return DiagnosticFact(key=key, state="needs_attention", summary="smoothed_wind.json exists but failed to parse.", detail=error, path=str(path), evidence={"exists": True})
    ts = _parse_semantic_timestamp(data.get("time")) if isinstance(data, dict) else None
    if ts is not None:
        return DiagnosticFact(
            key=key, state="ready", summary="smoothed_wind.json exists and parses.",
            path=str(path), timestamp=_iso(ts), age_seconds=_age_seconds(now, ts),
            evidence={"timestamp_source": "payload.time", "exists": True},
        )
    timestamp, age = _mtime_evidence(path, now)
    return DiagnosticFact(
        key=key, state="ready", summary="smoothed_wind.json exists and parses (no semantic timestamp found).",
        path=str(path), timestamp=timestamp, age_seconds=age,
        evidence={"timestamp_source": "file_mtime", "exists": True},
    )


def _check_forecast_cache(path: Path, now) -> DiagnosticFact:
    """Exposes wx_forecast.py's own existing 6-hour "cache is stale"
    warning as `degraded` evidence -- see FORECAST_CACHE_WARN_HOURS.
    Does not invent a new expiration/fallback rule."""
    key = "forecast_cache"
    data, error = _load_json_file(path)
    if error == "missing":
        # get_periods() only ever writes this file after a successful NWS
        # fetch -- absence on a fresh install/data-dir move means "hasn't
        # run yet", not a config problem, so this matches the other
        # not-yet-produced runtime evidence below (`degraded`, not
        # `needs_attention`).
        return DiagnosticFact(key=key, state="degraded", summary="No forecast cache exists yet.", path=str(path), evidence={"exists": False})
    if error:
        return DiagnosticFact(key=key, state="needs_attention", summary="Forecast cache exists but failed to parse.", detail=error, path=str(path), evidence={"exists": True})
    if not isinstance(data, list) or not data:
        return DiagnosticFact(key=key, state="needs_attention", summary="Forecast cache is not a non-empty list of periods.", path=str(path), evidence={"exists": True})

    timestamp, age = _mtime_evidence(path, now)
    age_hours = (age / 3600.0) if age is not None else None
    evidence = {"period_count": len(data), "timestamp_source": "file_mtime", "exists": True}
    if age_hours is not None and age_hours > FORECAST_CACHE_WARN_HOURS:
        return DiagnosticFact(
            key=key, state="degraded",
            summary=f"Forecast cache is {age_hours:.1f}h old, past the existing {FORECAST_CACHE_WARN_HOURS}h warning boundary.",
            path=str(path), count=len(data), timestamp=timestamp, age_seconds=age, evidence=evidence,
        )
    return DiagnosticFact(
        key=key, state="ready",
        summary=f"Forecast cache has {len(data)} period(s).",
        path=str(path), count=len(data), timestamp=timestamp, age_seconds=age, evidence=evidence,
    )


def _check_watch_warnings(path: Path, now) -> DiagnosticFact:
    key = "watch_warnings"
    data, error = _load_json_file(path)
    if error == "missing":
        # update_local_wx_data.py writes this file unconditionally on
        # every successful fetch cycle (even an empty list) -- absence
        # means the pipeline has not yet completed a cycle, not
        # necessarily a config problem, hence `degraded` not
        # `needs_attention`.
        return DiagnosticFact(key=key, state="degraded", summary="No watch/warning snapshot exists yet -- pipeline has not completed a cycle.", path=str(path), evidence={"exists": False})
    if error:
        return DiagnosticFact(key=key, state="needs_attention", summary="Watch/warning snapshot exists but failed to parse.", detail=error, path=str(path), evidence={"exists": True})
    if not isinstance(data, list):
        return DiagnosticFact(key=key, state="needs_attention", summary="Watch/warning snapshot is not a list.", path=str(path), evidence={"exists": True})
    events = [e.get("event") for e in data if isinstance(e, dict) and e.get("event")]
    timestamp, age = _mtime_evidence(path, now)
    return DiagnosticFact(
        key=key, state="ready",
        summary=f"{len(data)} active watch/warning(s)." if data else "No active watches/warnings.",
        path=str(path), count=len(data), timestamp=timestamp, age_seconds=age,
        evidence={"events": events, "timestamp_source": "file_mtime", "exists": True},
    )


def _check_amber_alerts_data(amber_cfg: AmberAlertConfig | None, path: Path, now) -> DiagnosticFact:
    key = "amber_alerts_data"
    # No row (never configured) behaves exactly like an explicit
    # enabled=False for this purpose: no active-alert snapshot is
    # expected either way, and neither case may be reported as if
    # AMBER were actively configured.
    if amber_cfg is None or not amber_cfg.enabled:
        return DiagnosticFact(
            key=key, state="optional_disabled",
            summary="AMBER-family alerts disabled -- no active-alert snapshot expected.",
        )
    data, error = _load_json_file(path)
    if error == "missing":
        return DiagnosticFact(key=key, state="degraded", summary="AMBER-family alerts enabled but no snapshot exists yet.", path=str(path), evidence={"exists": False})
    if error:
        return DiagnosticFact(key=key, state="needs_attention", summary="AMBER-family alert snapshot exists but failed to parse.", detail=error, path=str(path), evidence={"exists": True})
    if not isinstance(data, list):
        return DiagnosticFact(key=key, state="needs_attention", summary="AMBER-family alert snapshot is not a list.", path=str(path), evidence={"exists": True})
    events = [e.get("event") for e in data if isinstance(e, dict) and e.get("event")]
    timestamp, age = _mtime_evidence(path, now)
    return DiagnosticFact(
        key=key, state="ready",
        summary=f"{len(data)} active AMBER-family alert(s)." if data else "No active AMBER-family alerts.",
        path=str(path), count=len(data), timestamp=timestamp, age_seconds=age,
        evidence={"events": events, "timestamp_source": "file_mtime", "exists": True},
    )


# ---------------------------------
# Generated Weather artifacts
# ---------------------------------

def _confirmed_active_count(fact: DiagnosticFact, disabled_counts_as_zero: bool) -> int | None:
    """Extracts a TRUSTED active-count from an already-collected source
    fact (`watch_warnings` or `amber_alerts_data`), for
    _classify_alert_applicability() below. Returns None ("we cannot
    trust this source right now") unless the fact's own state proves a
    real count -- `degraded`/`needs_attention` never count as zero,
    since a missing/malformed snapshot tells us nothing about whether
    an alert is actually active. `disabled_counts_as_zero` is AMBER-
    only: `optional_disabled` (master switch off, or never configured)
    confidently means zero AMBER alerts, by construction -- the poller
    never runs at all."""
    if fact.state == "ready":
        return fact.count if fact.count is not None else 0
    if disabled_counts_as_zero and fact.state == "optional_disabled":
        return 0
    return None


def _classify_alert_applicability(watch_fact: DiagnosticFact, amber_data_fact: DiagnosticFact) -> str:
    """Combines the already-collected watch/warning and AMBER-family
    active-alert evidence into one verdict for the shared, event-driven
    WxAlert/wx_alert.mp3 artifact -- never re-reads or reinterprets the
    source files itself (see _check_generated_artifact()). One of:

      "active"    -- either source confidently shows count > 0
      "no_active" -- BOTH sources confidently show count == 0
      "unknown"   -- neither of the above could be established (a
                     source is missing/malformed/not-yet-run) -- must
                     never be reported as `not_applicable`, since that
                     would claim more certainty than the evidence has.
    """
    watch_count = _confirmed_active_count(watch_fact, disabled_counts_as_zero=False)
    amber_count = _confirmed_active_count(amber_data_fact, disabled_counts_as_zero=True)

    if (watch_count is not None and watch_count > 0) or (amber_count is not None and amber_count > 0):
        return "active"
    if watch_count == 0 and amber_count == 0:
        return "no_active"
    return "unknown"


def _check_generated_artifact(
    suffix, category_code, filename, event_driven, library_root: Path, now, alert_applicability: str | None = None,
) -> DiagnosticFact:
    """Inspects file-vs-Track agreement WITHOUT mutating anything --
    never creates a Track, never calls sync_track_file, never
    synthesizes. delivery.py already atomically publishes the file and
    runs sync_track_file together, so a disagreement here (file without
    a Track, or vice versa) is itself useful evidence of something
    interrupted between those two steps.

    `alert_applicability` (only meaningful when `event_driven`) is
    _classify_alert_applicability()'s verdict, computed once from the
    already-collected watch/warning + AMBER-data facts -- this function
    never independently decides whether an alert is active."""
    key = f"generated_artifact:{suffix}"
    expected_path = library_root / category_code / filename
    file_exists = expected_path.is_file()

    category = Category.objects.filter(code=category_code).first()
    track = None
    if category is not None:
        track = Track.objects.filter(category=category, filepath=str(expected_path)).first()

    evidence = {"category_code": category_code, "filename": filename, "event_driven": event_driven, "exists": file_exists}
    timestamp = age = None
    if file_exists:
        timestamp, age = _mtime_evidence(expected_path, now)
    if track is not None:
        evidence["track_id"] = track.id

    if event_driven:
        return _check_wx_alert_artifact(key, expected_path, file_exists, track, timestamp, age, evidence, alert_applicability)

    if not file_exists and track is None:
        return DiagnosticFact(
            key=key, state="degraded",
            summary=f"{filename} has not been generated yet.",
            path=str(expected_path), evidence=evidence,
        )
    if file_exists and track is None:
        return DiagnosticFact(
            key=key, state="needs_attention",
            summary=f"{filename} exists on disk but has no matching Track -- delivery may have been interrupted.",
            path=str(expected_path), timestamp=timestamp, age_seconds=age, evidence=evidence,
        )
    if not file_exists and track is not None:
        return DiagnosticFact(
            key=key, state="needs_attention",
            summary=f"A Track references {filename} but the file is missing on disk.",
            path=str(expected_path), evidence=evidence,
        )
    if not track.ready2air:
        return DiagnosticFact(
            key=key, state="needs_attention",
            summary=f"{filename}'s Track exists but is not marked Ready to Air.",
            path=str(expected_path), timestamp=timestamp, age_seconds=age,
            evidence=evidence,
        )
    return DiagnosticFact(
        key=key, state="ready",
        summary=f"{filename} is present and its Track is Ready to Air.",
        path=str(expected_path), timestamp=timestamp, age_seconds=age,
        evidence=evidence,
    )


def _check_wx_alert_artifact(key, expected_path, file_exists, track, timestamp, age, evidence, applicability) -> DiagnosticFact:
    """WxAlert/wx_alert.mp3 is the urgent spoken watch/warning and
    AMBER-family statement (built by weather_ingest/wx_alert.py and
    amber_alert.py) -- NOT the FX "alert beep" tracked separately by
    `alert_fx_cart`. It is shared by both source types and, being
    event-driven, a file/Track surviving from a past alert must not be
    read as a currently `ready` artifact once that alert has ended --
    see _classify_alert_applicability()."""
    evidence = {**evidence, "track_ready_to_air": track.ready2air if track is not None else None}

    if applicability == "unknown":
        return DiagnosticFact(
            key=key, state="degraded",
            summary="Current alert-artifact applicability cannot be established from source evidence.",
            detail="Watch/warning and/or AMBER-family source evidence is missing, malformed, or not yet "
                   "produced -- cannot confirm whether a Weather/AMBER alert is presently active.",
            path=str(expected_path) if file_exists else None,
            timestamp=timestamp, age_seconds=age, evidence=evidence,
        )

    if applicability == "no_active":
        if file_exists or track is not None:
            summary = "No active Weather/AMBER alert; historical wx_alert.mp3 is present."
        else:
            summary = "No active Weather/AMBER alert; wx_alert.mp3 has never been generated."
        return DiagnosticFact(
            key=key, state="not_applicable", summary=summary,
            path=str(expected_path) if file_exists else None,
            timestamp=timestamp, age_seconds=age, evidence=evidence,
        )

    # applicability == "active"
    if file_exists and track is not None and track.ready2air:
        return DiagnosticFact(
            key=key, state="ready",
            summary="An alert is active and wx_alert.mp3 is present with its Track Ready to Air.",
            path=str(expected_path), timestamp=timestamp, age_seconds=age, evidence=evidence,
        )
    if not file_exists and track is None:
        return DiagnosticFact(
            key=key, state="needs_attention",
            summary="An alert is active but wx_alert.mp3 has never been generated.",
            evidence=evidence,
        )
    if file_exists and track is None:
        return DiagnosticFact(
            key=key, state="needs_attention",
            summary="An alert is active but wx_alert.mp3 has no matching Track -- delivery may have been interrupted.",
            path=str(expected_path), timestamp=timestamp, age_seconds=age, evidence=evidence,
        )
    if not file_exists and track is not None:
        return DiagnosticFact(
            key=key, state="needs_attention",
            summary="An alert is active but wx_alert.mp3 is missing on disk (a Track still references it).",
            evidence=evidence,
        )
    # file_exists and track is not None and not track.ready2air
    return DiagnosticFact(
        key=key, state="needs_attention",
        summary="An alert is active but wx_alert.mp3's Track is not marked Ready to Air.",
        path=str(expected_path), timestamp=timestamp, age_seconds=age, evidence=evidence,
    )
