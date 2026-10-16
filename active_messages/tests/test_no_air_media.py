"""r0113 / D3C: NO-AIR (TEST) media can never be an active message's media
or an execution's aggregate -- proven at the binding authority itself, NOT
by native airing being closed (the refusal does not consult that gate)."""
import uuid
from unittest import mock

from django.test import TestCase

from active_messages import services
from production.models import ProductionMedia

from .support import Domain, media


class NoAirMessageMediaTests(Domain, TestCase):
    def test_no_air_media_is_refused_whatever_the_airing_gate_says(self):
        message = services.prepare_message(actor=self.producer, content_type=self.pa_type)
        no_air = media(self.producer, airing_prohibited=True)
        for gate in (False, True):
            with self.subTest(native_airing_available=gate), \
                    mock.patch.object(services, "NATIVE_AIRING_AVAILABLE", gate), \
                    self.assertRaises(services.MediaRefused) as raised:
                services.bind_message_media(message, no_air, actor=self.producer)
            self.assertEqual(raised.exception.code, "airing_prohibited")
        message.refresh_from_db()
        self.assertIsNone(message.message_media_id)
        self.assertNotIn("media_bound", self.events(message))

    def test_control_cannot_bind_it_either(self):
        message = services.prepare_message(actor=self.producer, content_type=self.pa_type)
        with self.assertRaises(services.MediaRefused):
            services.bind_message_media(message, media(self.producer, airing_prohibited=True), actor=self.controller)

    def test_an_aggregate_derived_from_no_air_media_is_never_bound(self):
        source = media(self.producer, airing_prohibited=True)
        mix = media(None, kind=ProductionMedia.KIND_RENDITION, derived_from=source, airing_prohibited=True)
        with self.assertRaises(services.MixRefused) as raised:
            services.bind_execution_mix(uuid.uuid4(), mix)
        # a permanent FAILURE_CODES failure, refused at the binding before any execution is touched
        self.assertEqual(raised.exception.code, "input_unavailable")
        self.assertIn(raised.exception.code, services.FAILURE_CODES)
        self.assertIn("NO AIR", str(raised.exception))

    def test_ordinary_media_still_binds(self):
        message = self.prepared(self.producer)
        self.assertIsNotNone(message.message_media_id)
