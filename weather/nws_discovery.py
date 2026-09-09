"""Operator-triggered NWS point discovery (Pass D, P1 1.15/2.4).

Deliberately has NO Django imports -- this module only knows how to
call api.weather.gov's /points endpoint and return typed, parsed
results or raise a typed error; weather/admin.py's discovery view is
what turns this into an Admin workflow (fetch on an explicit POST,
render a compare-and-apply page, write WeatherConfig only on a second
explicit POST). Keeping the two separate is what makes this mockable
in tests with no live network -- see weather/tests/test_nws_discovery.py.

This is assistive discovery only, never a new runtime authority: the
values weather_ingest actually reads still come from WeatherConfig via
dump_weather_config, unchanged. Nothing here is called from that
runtime path, on a timer, or automatically -- only from an explicit
Admin button.

User-Agent matches the one weather_ingest/wx_forecast.py already sends
to api.weather.gov (NWS's own API usage policy asks for an identifying
UA + contact) -- same station, same convention, not a new one.
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

NWS_POINTS_URL_TEMPLATE = "https://api.weather.gov/points/{lat},{lon}"

NWS_HEADERS = {
    "User-Agent": "OakGroveRadio WX Bot (oakgroveradio@gmail.com)",
    "Accept": "application/geo+json",
}

# A bounded connect + read timeout -- an Admin operator waiting on a
# button click should never hang indefinitely; matches the general
# shape of road_conditions/api.py's own NWS-adjacent-government-API
# client (see that module's own comment on why connect vs. read are
# split), scaled down since this is a single small request, not a
# multi-KB sync.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5
DEFAULT_READ_TIMEOUT_SECONDS = 10

# Retry only transient connection-level failures/5xx, never a 4xx (a
# bad lat/lon or a genuinely wrong URL is a permanent problem, not
# worth retrying), and never on read timeout (a slow-but-working
# response shouldn't be fetched twice) -- same policy as
# road_conditions/api.py's own _RETRY.
_RETRY = Retry(
    total=2,
    connect=2,
    read=0,
    status=2,
    backoff_factor=1.0,
    status_forcelist=(502, 503, 504),
    allowed_methods=frozenset({"GET"}),
    raise_on_status=False,
)


class NWSDiscoveryError(Exception):
    """Base for every error this module raises -- always a clear,
    human-readable message and never a raw traceback/500 for the
    Admin operator to see."""


def _build_session():
    session = requests.Session()
    session.headers.update(NWS_HEADERS)
    adapter = HTTPAdapter(max_retries=_RETRY)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _last_path_segment(url):
    """'https://api.weather.gov/zones/county/KSC143' -> 'KSC143'.
    Both the county-zone and forecast-zone resources in a /points
    response are URLs whose stable UGC identifier is their final path
    segment -- this is the ONLY place that identifier is derived from,
    so a NWS resource URL shape change would fail loudly (see the
    missing-fields check in fetch_nws_point) rather than silently
    producing a wrong code."""
    return (url or "").rstrip("/").rsplit("/", 1)[-1] or None


def fetch_nws_point(lat, lon, session=None):
    """Discover WFO/grid/county-UGC for a station's coordinates via
    NWS's own /points authority. Returns a dict:

        {"grid_id": "TOP", "grid_x": 10, "grid_y": 53,
         "county_ugc": "KSC143", "forecast_zone_ugc": "KSZ004"}

    `forecast_zone_ugc` is informational only -- see the Pass D brief's
    explicit instruction that a county UGC (used for alert lookup) must
    never be silently replaced by a forecast-zone UGC. Raises
    NWSDiscoveryError (never a raw requests/JSON exception) on any
    connection failure, timeout, non-2xx response, malformed JSON, or
    a response missing an expected field."""
    url = NWS_POINTS_URL_TEMPLATE.format(lat=lat, lon=lon)
    session = session or _build_session()
    try:
        resp = session.get(url, timeout=(DEFAULT_CONNECT_TIMEOUT_SECONDS, DEFAULT_READ_TIMEOUT_SECONDS))
    except requests.exceptions.Timeout as exc:
        raise NWSDiscoveryError(f"Timed out calling api.weather.gov ({exc}).") from exc
    except requests.exceptions.ConnectionError as exc:
        raise NWSDiscoveryError(f"Could not connect to api.weather.gov ({exc}).") from exc
    except requests.exceptions.RequestException as exc:
        raise NWSDiscoveryError(f"Request to api.weather.gov failed: {exc}") from exc

    if not resp.ok:
        raise NWSDiscoveryError(f"api.weather.gov returned HTTP {resp.status_code}.")

    try:
        data = resp.json()
    except ValueError as exc:
        raise NWSDiscoveryError("api.weather.gov returned malformed JSON.") from exc

    if not isinstance(data, dict):
        raise NWSDiscoveryError("api.weather.gov response was not a JSON object.")
    props = data.get("properties")
    if not isinstance(props, dict):
        raise NWSDiscoveryError("api.weather.gov response is missing a 'properties' object.")

    grid_id = props.get("gridId")
    grid_x = props.get("gridX")
    grid_y = props.get("gridY")
    county_ugc = _last_path_segment(props.get("county"))
    forecast_zone_ugc = _last_path_segment(props.get("forecastZone"))

    missing = [
        name for name, value in (
            ("gridId", grid_id), ("gridX", grid_x), ("gridY", grid_y), ("county", county_ugc),
        ) if not value and value != 0
    ]
    if missing:
        raise NWSDiscoveryError(
            f"api.weather.gov response is missing expected field(s): {', '.join(missing)}."
        )

    try:
        grid_x = int(grid_x)
        grid_y = int(grid_y)
    except (TypeError, ValueError) as exc:
        raise NWSDiscoveryError("api.weather.gov returned non-numeric grid coordinates.") from exc

    return {
        "grid_id": str(grid_id),
        "grid_x": grid_x,
        "grid_y": grid_y,
        "county_ugc": county_ugc,
        "forecast_zone_ugc": forecast_zone_ugc,
    }
