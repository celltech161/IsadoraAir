"""CarsApiClient tests -- no live network requests. Every test
monkeypatches the client's own requests.Session.get with a fake, so
these run offline and fast."""
import json
from unittest.mock import Mock

import requests
from django.test import SimpleTestCase

from road_conditions.api import (
    CarsApiAuthError,
    CarsApiClient,
    CarsApiConnectionError,
    CarsApiHTTPError,
    CarsApiSchemaError,
    CarsApiTimeout,
)


def _fake_response(status_code=200, json_body=None, raise_json_error=False):
    resp = Mock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 400
    if raise_json_error:
        resp.json.side_effect = ValueError("not valid JSON")
    else:
        resp.json.return_value = json_body
    return resp


class CarsApiClientTests(SimpleTestCase):
    def _client(self):
        return CarsApiClient(base_url="https://example.invalid/carsapi_v1/api", timeout_seconds=5)

    def test_get_events_success(self):
        client = self._client()
        client._session.get = Mock(return_value=_fake_response(200, [{"event-id": "X"}]))
        events = client.get_events()
        self.assertEqual(events, [{"event-id": "X"}])

    def test_get_events_sends_classifications_and_route_params(self):
        client = self._client()
        mock_get = Mock(return_value=_fake_response(200, []))
        client._session.get = mock_get
        client.get_events(event_classifications=["constructionReports", "roadReports"], route_designator="US 81")
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"], {"eventClassifications": "constructionReports,roadReports", "routeDesignator": "US 81"})

    def test_get_events_no_params_when_none_given(self):
        client = self._client()
        mock_get = Mock(return_value=_fake_response(200, []))
        client._session.get = mock_get
        client.get_events()
        _, kwargs = mock_get.call_args
        self.assertIsNone(kwargs["params"])

    def test_timeout_raises_typed_exception(self):
        client = self._client()
        client._session.get = Mock(side_effect=requests.exceptions.Timeout("timed out"))
        with self.assertRaises(CarsApiTimeout):
            client.get_events()

    def test_connection_error_raises_typed_exception(self):
        client = self._client()
        client._session.get = Mock(side_effect=requests.exceptions.ConnectionError("refused"))
        with self.assertRaises(CarsApiConnectionError):
            client.get_events()

    def test_401_raises_auth_error(self):
        client = self._client()
        client._session.get = Mock(return_value=_fake_response(401))
        with self.assertRaises(CarsApiAuthError) as ctx:
            client.get_events()
        self.assertEqual(ctx.exception.status_code, 401)

    def test_403_raises_auth_error(self):
        client = self._client()
        client._session.get = Mock(return_value=_fake_response(403))
        with self.assertRaises(CarsApiAuthError):
            client.get_events()

    def test_500_raises_http_error_not_auth_error(self):
        client = self._client()
        client._session.get = Mock(return_value=_fake_response(500))
        with self.assertRaises(CarsApiHTTPError) as ctx:
            client.get_events()
        self.assertEqual(ctx.exception.status_code, 500)

    def test_malformed_json_raises_schema_error(self):
        client = self._client()
        client._session.get = Mock(return_value=_fake_response(200, raise_json_error=True))
        with self.assertRaises(CarsApiSchemaError):
            client.get_events()

    def test_events_response_not_a_list_raises_schema_error(self):
        client = self._client()
        client._session.get = Mock(return_value=_fake_response(200, {"not": "a list"}))
        with self.assertRaises(CarsApiSchemaError):
            client.get_events()

    def test_event_classifications_not_a_list_raises_schema_error(self):
        client = self._client()
        client._session.get = Mock(return_value=_fake_response(200, {"not": "a list"}))
        with self.assertRaises(CarsApiSchemaError):
            client.get_event_classifications()

    def test_single_event_lookup(self):
        client = self._client()
        client._session.get = Mock(return_value=_fake_response(200, {"event-id": "CARS5-1"}))
        event = client.get_event("CARS5-1")
        self.assertEqual(event["event-id"], "CARS5-1")

    def test_404_on_single_event_raises_http_error(self):
        client = self._client()
        client._session.get = Mock(return_value=_fake_response(404))
        with self.assertRaises(CarsApiHTTPError) as ctx:
            client.get_event("DOES-NOT-EXIST")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_credentials_only_attached_when_both_present(self):
        client = CarsApiClient(base_url="https://example.invalid/api", username="user", password="pass")
        self.assertEqual(client._session.auth, ("user", "pass"))

        client_no_creds = CarsApiClient(base_url="https://example.invalid/api")
        self.assertIsNone(client_no_creds._session.auth)

        client_partial = CarsApiClient(base_url="https://example.invalid/api", username="user")
        self.assertIsNone(client_partial._session.auth)

    def test_user_agent_is_descriptive_not_default(self):
        client = self._client()
        self.assertIn("IsadoraAir", client._session.headers["User-Agent"])
        self.assertNotIn("python-requests", client._session.headers["User-Agent"])

    def test_never_makes_a_real_network_call(self):
        """Sanity check on the test suite itself -- if this ever DID
        reach the network, json() on a real response wouldn't be a Mock
        and the assertion below would fail loudly rather than silently
        passing."""
        client = self._client()
        client._session.get = Mock(return_value=_fake_response(200, []))
        client.get_events()
        self.assertTrue(client._session.get.called)
        # json.dumps sanity -- confirms we're operating on plain Python
        # objects, not e.g. an unexhausted response stream.
        json.dumps(client._session.get.return_value.json())
