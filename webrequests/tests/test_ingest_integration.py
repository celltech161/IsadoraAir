"""Focused coverage for the first-class public-site request integration."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from django.conf import settings
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from library.models import Artist, Category, CategoryKind, Track
from monitoring.models import SystemEvent
from webrequests.ingest import (
    AUTH_HEADER,
    PendingRequestValidationError,
    PublicSiteClient,
    RemoteProtocolError,
    RemoteSiteError,
    WebRequestConfigurationError,
    ingest_pending_items,
    report_failure,
)
from webrequests.management.commands.ingest_web_requests import Command as IngestCommand
from webrequests.management.commands.sync_web_request_catalog import Command as CatalogCommand
from webrequests.models import SongRequest, WebRequestConfig


class FakeResponse:
    def __init__(self, payload=None, *, status=200, content_type="application/json", raw=None, headers=None):
        self.status_code = status
        self._raw = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for offset in range(0, len(self._raw), chunk_size):
            yield self._raw[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class PublicSiteClientTests(SimpleTestCase):
    def test_authentication_is_header_only_and_timeouts_are_explicit(self):
        session = MagicMock()
        session.request.return_value = FakeResponse({"success": True, "requests": []})
        client = PublicSiteClient(
            "https://radio.example", "super-secret-value",
            connect_timeout=3, read_timeout=9, session=session,
        )

        self.assertEqual(client.fetch_pending_requests(), [])

        _args, kwargs = session.request.call_args
        self.assertEqual(kwargs["headers"][AUTH_HEADER], "super-secret-value")
        self.assertNotIn(b"super-secret-value", kwargs["data"] or b"")
        self.assertNotIn("super-secret-value", session.request.call_args.args[1])
        self.assertEqual(kwargs["timeout"], (3, 9))
        self.assertTrue(kwargs["stream"])

    def test_connection_failure_is_safe_and_does_not_expose_key(self):
        session = MagicMock()
        session.request.side_effect = requests.Timeout("transport details")
        client = PublicSiteClient("https://radio.example", "never-log-this", session=session)

        with self.assertRaises(RemoteSiteError) as caught:
            client.fetch_pending_requests()

        self.assertNotIn("never-log-this", str(caught.exception))

    def test_malformed_response_is_rejected(self):
        session = MagicMock()
        session.request.return_value = FakeResponse(raw=b"not-json")
        client = PublicSiteClient("https://radio.example", "secret", session=session)

        with self.assertRaises(RemoteProtocolError):
            client.fetch_pending_requests()

    def test_oversized_response_is_rejected_before_json_parsing(self):
        session = MagicMock()
        session.request.return_value = FakeResponse(
            {"success": True, "requests": []}, headers={"Content-Length": "1001"},
        )
        client = PublicSiteClient(
            "https://radio.example", "secret", max_response_bytes=1000, session=session,
        )

        with self.assertRaises(RemoteProtocolError):
            client.fetch_pending_requests()

    def test_missing_or_insecure_configuration_fails_clearly(self):
        with self.assertRaises(WebRequestConfigurationError):
            PublicSiteClient("", "secret")
        with self.assertRaises(WebRequestConfigurationError):
            PublicSiteClient("http://radio.example", "secret")
        with self.assertRaises(WebRequestConfigurationError):
            PublicSiteClient("https://radio.example", "")

    def test_execution_is_bounded_in_http_and_systemd(self):
        ingest_unit = (Path(settings.BASE_DIR) / "deploy/isadoraair-web-requests-ingest.service").read_text()
        catalog_unit = (Path(settings.BASE_DIR) / "deploy/isadoraair-web-requests-catalog.service").read_text()
        self.assertIn("TimeoutStartSec=90", ingest_unit)
        self.assertIn("TimeoutStartSec=120", catalog_unit)
        self.assertGreater(settings.WEB_REQUESTS_INGEST_CONNECT_TIMEOUT, 0)
        self.assertGreater(settings.WEB_REQUESTS_INGEST_READ_TIMEOUT, 0)

    def test_implementation_contains_no_oak_grove_host_or_home_path(self):
        root = Path(settings.BASE_DIR)
        paths = [
            root / "webrequests/ingest.py",
            root / "webrequests/management/commands/ingest_web_requests.py",
            root / "webrequests/management/commands/sync_web_request_catalog.py",
            root / "deploy/isadoraair-web-requests-ingest.service",
            root / "deploy/isadoraair-web-requests-catalog.service",
        ]
        implementation = "\n".join(path.read_text() for path in paths).lower()
        self.assertNotIn("oakgrove", implementation)
        self.assertNotIn("/home/jreed", implementation)


class IngestFixtureMixin:
    def setUp(self):
        super().setUp()
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.category = Category.objects.create(code="INGESTTEST", name="Ingest Test", kind=kind)
        self.artist = Artist.objects.create(name="Ingest Artist")
        self.track = Track.objects.create(
            filepath="/tmp/webrequest-ingest-test.mp3",
            filename="webrequest-ingest-test.mp3",
            title="Request Me",
            artist=self.artist,
            category=self.category,
            ready2air=True,
            duration_seconds=180,
        )
        WebRequestConfig.objects.all().delete()
        self.cfg = WebRequestConfig.objects.create(enabled=True, open_slots=list(range(168)))

    def item(self, external_id="remote-1"):
        return {
            "external_request_id": external_id,
            "track_id": self.track.pk,
            "requester_name": "Alex",
            "dedication_message": "For everyone listening",
            "submitted_at": timezone.now().isoformat(),
        }


class PendingImportTests(IngestFixtureMixin, TestCase):
    def test_valid_remote_request_becomes_existing_song_request_model(self):
        result = ingest_pending_items([self.item()])

        self.assertEqual(result, {"created": 1, "skipped": 0})
        request = SongRequest.objects.get(external_request_id="remote-1")
        self.assertEqual(request.track, self.track)
        self.assertEqual(request.requester_name, "Alex")
        self.assertEqual(request.status, "pending")

    def test_duplicate_delivery_does_not_create_duplicate(self):
        ingest_pending_items([self.item()])
        result = ingest_pending_items([self.item()])

        self.assertEqual(result, {"created": 0, "skipped": 1})
        self.assertEqual(SongRequest.objects.filter(external_request_id="remote-1").count(), 1)

    def test_malformed_batch_is_validated_before_any_insert(self):
        malformed = self.item("remote-bad")
        malformed["submitted_at"] = "not-a-timestamp"

        with self.assertRaises(PendingRequestValidationError):
            ingest_pending_items([self.item("remote-good"), malformed])

        self.assertFalse(SongRequest.objects.exists())


@override_settings(
    WEB_REQUESTS_INGEST_URL="https://radio.example",
    WEB_REQUESTS_INGEST_API_KEY="test-key",
)
class ManagementCommandTests(IngestFixtureMixin, TestCase):
    def _client(self, items=None):
        client = MagicMock()
        client.fetch_pending_requests.return_value = items if items is not None else []
        client.push_status_updates.return_value = 1
        return client

    def test_disabled_ingest_performs_no_remote_request(self):
        self.cfg.enabled = False
        self.cfg.save(update_fields=["enabled"])
        with patch(
            "webrequests.management.commands.ingest_web_requests.PublicSiteClient.from_settings"
        ) as factory:
            IngestCommand().handle()
        factory.assert_not_called()

    def test_unconfigured_station_is_a_read_only_no_op(self):
        WebRequestConfig.objects.all().delete()
        with patch(
            "webrequests.management.commands.ingest_web_requests.PublicSiteClient.from_settings"
        ) as factory:
            IngestCommand().handle()
        factory.assert_not_called()
        self.assertFalse(WebRequestConfig.objects.exists())

    def test_successful_ingest_acknowledges_with_status_post(self):
        client = self._client([self.item()])
        with (
            patch(
                "webrequests.management.commands.ingest_web_requests.PublicSiteClient.from_settings",
                return_value=client,
            ),
            patch("webrequests.management.commands.ingest_web_requests.call_command"),
        ):
            IngestCommand().handle()

        updates = client.push_status_updates.call_args.args[0]
        self.assertEqual(updates[0]["external_request_id"], "remote-1")
        self.assertEqual(updates[0]["status"], "pending")

    def test_failed_database_insert_does_not_acknowledge(self):
        client = self._client([self.item()])
        with (
            patch(
                "webrequests.management.commands.ingest_web_requests.PublicSiteClient.from_settings",
                return_value=client,
            ),
            patch(
                "webrequests.management.commands.ingest_web_requests.ingest_pending_items",
                side_effect=DatabaseError("insertion failed"),
            ),
            patch("webrequests.management.commands.ingest_web_requests.report_failure"),
            self.assertRaises(CommandError),
        ):
            IngestCommand().handle()
        client.push_status_updates.assert_not_called()

    def test_remote_timeout_fails_cleanly_for_timer_retry(self):
        client = self._client()
        client.fetch_pending_requests.side_effect = RemoteSiteError("pending request timed out")
        with (
            patch(
                "webrequests.management.commands.ingest_web_requests.PublicSiteClient.from_settings",
                return_value=client,
            ),
            patch("webrequests.management.commands.ingest_web_requests.report_failure") as report,
            self.assertRaises(CommandError),
        ):
            IngestCommand().handle()
        report.assert_called_once()
        client.push_status_updates.assert_not_called()

    def test_existing_refresh_command_remains_authoritative(self):
        client = self._client([self.item()])
        with (
            patch(
                "webrequests.management.commands.ingest_web_requests.PublicSiteClient.from_settings",
                return_value=client,
            ),
            patch("webrequests.management.commands.ingest_web_requests.call_command") as refresh,
        ):
            IngestCommand().handle()
        self.assertEqual(refresh.call_args.args[0], "refresh_song_request_statuses")

    def test_disabled_catalog_sync_has_no_side_effects(self):
        self.cfg.enabled = False
        self.cfg.save(update_fields=["enabled"])
        with patch(
            "webrequests.management.commands.sync_web_request_catalog.PublicSiteClient.from_settings"
        ) as factory:
            CatalogCommand().handle()
        factory.assert_not_called()

    def test_repeated_outage_events_are_coalesced(self):
        with patch("webrequests.ingest.send_mail") as send:
            report_failure("ingest", RemoteSiteError("site unavailable"), cfg=self.cfg)
            report_failure("ingest", RemoteSiteError("site unavailable"), cfg=self.cfg)
        event = SystemEvent.objects.get(dedupe_key="webrequests|ingest")
        self.assertEqual(event.repeat_count, 2)
        # notify_email is blank in this fixture, so neither outage leaks into mail.
        send.assert_not_called()
