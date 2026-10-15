"""iPortal D3B in headless Chromium: the REAL shared workstation, opened on one
exact active-message draft, records from a fake capture device and saves --
and saving binds audio to the draft without publishing it. Hardware-free as
production.tests.test_recorder_browser (fake device, no audio output)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import Group
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, override_settings

from active_messages import services
from active_messages.iportal import NATIVE_AIRING_CLOSED
from active_messages.models import ActiveBroadcastMessage
from library.models import GroupAccess
from production.tests.support import ffmpeg_available
from production.tests.test_recorder_browser import _tone, in_browser, sync_playwright

from .support import Domain, assign, content_type


@unittest.skipIf(sync_playwright is None or not ffmpeg_available(), "Playwright/ffmpeg not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class ActiveMessageWorkstationBrowserTests(Domain, StaticLiveServerTestCase):
    serialized_rollback = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.assets = tempfile.TemporaryDirectory()
        assets = Path(cls.assets.name)
        cls.mic_tone = assets / "mic.wav"
        _tone(cls.mic_tone, 30)
        (assets / "empty-alsa.conf").write_text("")
        cls.browser_env = {**os.environ, "ALSA_CONFIG_PATH": str(assets / "empty-alsa.conf"),
                           "PULSE_SERVER": "unix:/nonexistent/pulse", "PULSE_RUNTIME_PATH": str(assets)}
        cls.browser_args = [
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={cls.mic_tone}",
            "--disable-audio-output", "--mute-audio", "--autoplay-policy=no-user-gesture-required",
        ]

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"DJANGO_ALLOW_ASYNC_UNSAFE": "true"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for group in Group.objects.filter(name__startswith="test-"):
            GroupAccess.objects.update_or_create(group=group, defaults={"allowed_prefixes": "/iportal/"})
        self.notice = content_type("town_notice", "public_address")
        assign(self.producer, self.notice)
        client = Client()
        client.force_login(self.producer)                       # an ordinary (non-staff) producer
        self.session_cookie = client.cookies["sessionid"].value

    def open(self, message):
        context = self.browser.new_context()
        context.add_cookies([{"name": "sessionid", "value": self.session_cookie, "url": self.live_server_url}])
        page = context.new_page()
        page.errors, page.requests = [], []
        page.on("pageerror", lambda exc: page.errors.append(str(exc)))
        page.on("request", lambda req: page.requests.append(req.url))
        page.goto(f"{self.live_server_url}/iportal/messages/record/?message={message.pk}")
        page.wait_for_function("window.IPortalWorkstation !== undefined")
        return page

    @in_browser
    def test_record_and_save_binds_the_draft_without_publishing_it(self):
        message = services.prepare_message(actor=self.producer, content_type=self.notice, title="Water main")
        page = self.open(message)
        self.assertEqual(page.text_content("#ipAirBadge"), "DRAFT AUDIO")
        self.assertEqual(page.text_content("#ipSave"), "Save audio to this draft")
        self.assertIn(NATIVE_AIRING_CLOSED, page.text_content("#ipPurpose"))
        self.assertIn("No audio saved to this draft yet.", page.text_content("#ipAirCurrent"))
        page.click("#ipArm")
        page.wait_for_function("window.IPortalWorkstation.recorder && window.IPortalWorkstation.recorder.state === 'armed'")
        page.click("#ipRecord")
        page.wait_for_timeout(1500)
        page.click("#ipStop")
        page.wait_for_function("window.IPortalWorkstation.recorder.state !== 'stopping' && window.IPortalWorkstation.pcm")
        page.click("#ipSave")
        page.wait_for_selector("#ipMessages .ip-banner.ok >> text=Saved to this draft", timeout=60000)
        self.assertIn("not active until you publish", page.text_content("#ipMessages"))
        stored = ActiveBroadcastMessage.objects.get(pk=message.pk)
        self.assertEqual(stored.state, "prepared")                              # Save ≠ Publish
        self.assertIsNotNone(stored.message_media_id)
        self.assertEqual(stored.message_media.owner_id, self.producer.pk)
        self.assertEqual(page.errors, [])
        self.assertFalse([url for url in page.requests if "/api/engine" in url or "/api/fx" in url])

    @in_browser
    def test_an_active_message_opens_read_only(self):
        message = services.prepare_message(actor=self.producer, content_type=self.notice)
        from .support import media
        services.bind_message_media(message, media(self.producer, seconds="3.000000"), actor=self.producer)
        services.activate_message(message, actor=self.producer)
        page = self.open(message)
        self.assertEqual(page.text_content("#ipAirBadge"), "ACTIVE")
        self.assertFalse(page.is_hidden("#ipBlocked"))
        self.assertIn("prepare a replacement", page.text_content("#ipBlocked"))
        self.assertTrue(page.is_disabled("#ipImportBtn"))
        self.assertTrue(page.is_disabled("#ipSave"))
        self.assertEqual(page.errors, [])
