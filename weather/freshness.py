"""Weather freshness policy -- P1 2.4 Pass G.

Centralizes the age-based staleness thresholds weather.diagnostics
applies to routine (non-event-driven) Weather evidence, so policy
lives in exactly one named place instead of being scattered across ad
hoc age comparisons in diagnostics.py. See weather/diagnostics.py's own
module docstring for the evidence-first design rule this builds on --
this module still doesn't collect any evidence itself, it only names
the boundaries diagnostics.py applies to evidence it already collects.

Cadence -> threshold rationale (current production cadences, 2026-09):

  - Local derived Weather-source data -- latest_weather.json,
    wind_history.json, smoothed_wind.json, sky_condition.json,
    processed_weather.json -- is refreshed by update_local_wx_data.py
    every 5 minutes. A 15-minute threshold (3x cadence) tolerates one
    missed cycle without false-positive noise, while still catching a
    genuinely stuck pipeline well within a Monitoring debounce window.
  - WxTemp (current_temp.py) is refreshed every 15 minutes. A
    30-minute threshold (2x cadence) follows the same one-missed-cycle
    reasoning.
  - WxObs (wx_forecast.py --mode 1day) has a roughly 2-hour maximum
    normal gap between generations. A 3-hour threshold gives the same
    one-cycle margin.
  - WxForecast (wx_forecast.py --mode 3day) runs effectively every 3
    hours. A 4-hour threshold mirrors that same margin.

These are product defaults, not operator-editable knobs -- per the
Pass G brief, do not make these user-editable merely to avoid choosing
a default. If repository evidence later shows a better boundary,
change the constant here (and this comment) rather than reinventing a
second policy elsewhere.

WxAlert (event-driven) deliberately has NO freshness threshold here --
its "not_applicable when nothing is currently active" semantics are
entirely handled by diagnostics.py's own
_classify_alert_applicability(), never by an age comparison. The
six-hour forecast-cache boundary (wx_forecast.py's own pre-existing
warning, made authoritative in this same pass) also stays a separate,
already-named constant in diagnostics.py
(FORECAST_CACHE_WARN_HOURS) -- it governs whether a CACHED forecast is
still usable as a broadcast source at all, a different question from
"is the currently-published WxForecast artifact fresh", which is what
WX_FORECAST_FRESHNESS_SECONDS below answers.
"""
from __future__ import annotations

# All thresholds in seconds.
CURRENT_DERIVED_FRESHNESS_SECONDS = 15 * 60
WX_TEMP_FRESHNESS_SECONDS = 30 * 60
WX_OBS_FRESHNESS_SECONDS = 3 * 60 * 60
WX_FORECAST_FRESHNESS_SECONDS = 4 * 60 * 60


def is_stale(age_seconds: float | None, threshold_seconds: float) -> bool:
    """True if `age_seconds` is known and exceeds `threshold_seconds`.
    An unknown age (None -- no semantic timestamp or file mtime could
    be established at all) is never considered stale here; a caller's
    existing missing/malformed-evidence handling already reports that
    case under its own, more specific state."""
    return age_seconds is not None and age_seconds > threshold_seconds
