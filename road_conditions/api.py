"""Minimal, typed HTTP client for the Kansas DOT CARS road-conditions
API (https://kscars.kandrive.gov/carsapi_v1/api -- the
ks.carsprogram.org console redirects here; verified live 2026-08-04).

Deliberately has NO Django imports -- this module only knows how to
talk to the CARS API and raise typed exceptions; road_conditions.services
is what turns its output into RoadEvent rows. Keeping the two separate
is what makes this mockable in tests with no live network (see
tests/fixtures/) and keeps HTTP concerns out of anything the audio
engine's GLib loop could ever touch -- nothing in this app is imported
by library/services/engine.py.

Auth: the API's Swagger spec declares HTTP Basic on every endpoint, but
anonymous access was verified working for every endpoint this project
uses -- no credentials are wired in from Django settings in this
version (see services.build_client()). `username`/`password` remain
accepted constructor arguments here, tested (see
tests/test_api_client.py's CarsApiClientTests.
test_credentials_only_attached_when_both_present), and only ever
handed to `requests.Session.auth` -- never logged, printed, or
included in any exception message anywhere in this module. Wire real
settings back into services.build_client() if KDOT ever requires auth.
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "IsadoraAir-RoadConditions/1.0 (Oak Grove Radio 98.5; road-conditions sync; contact kansasaerial@gmail.com)"

# Fixed, not admin-configurable (see RoadConditionsConfiguration.request_timeout_seconds
# help text) -- a slow DNS/TCP handshake should fail fast regardless of
# how patient the operator is willing to be for the response body.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5

# Retry only on connection-level failures and the classic transient
# server codes (502/503/504) -- appropriate for a government
# information API that occasionally hiccups, but never retried on a
# 4xx (that's a real client-side/permanent problem, not transient) and
# never retried once bytes of a response have started arriving (no
# retry on read timeout -- a slow-but-working response shouldn't be
# fetched twice).
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


class CarsApiError(Exception):
    """Base for every error this client raises."""


class CarsApiTimeout(CarsApiError):
    pass


class CarsApiConnectionError(CarsApiError):
    pass


class CarsApiAuthError(CarsApiError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class CarsApiHTTPError(CarsApiError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class CarsApiSchemaError(CarsApiError):
    """The response was valid JSON-over-HTTP but not shaped the way
    this client expects (e.g. an object where an array was required).
    Distinct from a parse failure on one record inside a valid list --
    that's handled per-record by services.py, not here."""


class CarsApiClient:
    """Thin wrapper over a requests.Session. One instance per sync run
    is fine -- cheap to construct, and reusing the session across the
    handful of calls in one run still gets connection-pooling/keep-
    alive benefit without needing to be a long-lived singleton."""

    def __init__(self, base_url, timeout_seconds=20,
                 connect_timeout_seconds=DEFAULT_CONNECT_TIMEOUT_SECONDS,
                 username="", password="", session=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = (connect_timeout_seconds, timeout_seconds)
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = USER_AGENT
        self._session.headers["Accept"] = "application/json"
        if username and password:
            self._session.auth = (username, password)
        adapter = HTTPAdapter(max_retries=_RETRY)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _get(self, path, params=None):
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            raise CarsApiTimeout(f"Timed out calling {url}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise CarsApiConnectionError(f"Connection error calling {url}") from exc
        except requests.exceptions.RequestException as exc:
            raise CarsApiError(f"Request to {url} failed: {exc}") from exc

        if resp.status_code in (401, 403):
            raise CarsApiAuthError(f"{url} returned HTTP {resp.status_code}", status_code=resp.status_code)
        if not resp.ok:
            raise CarsApiHTTPError(f"{url} returned HTTP {resp.status_code}", status_code=resp.status_code)

        try:
            return resp.json()
        except ValueError as exc:
            raise CarsApiSchemaError(f"{url} did not return valid JSON") from exc

    def get_events(self, event_classifications=None, route_designator=None):
        """GET /events -- active and future CARS events. No pagination
        exists on this endpoint (verified live: the full ~231-event,
        ~8MB dataset returns in one response) -- this always returns
        the complete result set for the given filters, never a page."""
        params = {}
        if event_classifications:
            params["eventClassifications"] = ",".join(event_classifications)
        if route_designator:
            params["routeDesignator"] = route_designator
        data = self._get("/events", params=params or None)
        if not isinstance(data, list):
            raise CarsApiSchemaError("GET /events did not return a JSON array")
        return data

    def get_event(self, event_id):
        """GET /events/{event-id} -- a single event by its stable id."""
        return self._get(f"/events/{event_id}")

    def get_event_classifications(self):
        """GET /eventClassifications -- the live enumeration of valid
        `eventClassifications` filter values for this deployment."""
        data = self._get("/eventClassifications")
        if not isinstance(data, list):
            raise CarsApiSchemaError("GET /eventClassifications did not return a JSON array")
        return data

    def get_version(self):
        """GET /version -- cheap connectivity/liveness probe."""
        return self._get("/version")
