"""iPortal D3C in headless Chromium: the REAL shared workstation on (1) one
exact regular DRAFT -- its save is the explicit "Submit for processing", which
validates the item and leaves processing to the worker -- and (2) a TEST / NO
AIR workspace, whose save keeps a test take and creates nothing airable.
Hardware-free as production.tests.test_recorder_browser (fake capture device,
no audio output)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, override_settings

from library.models import Track
from produced_content import regular
from produced_content.models import ProducedContentTestTake
from production.tests.support import ffmpeg_available
from production.tests.test_recorder_browser import _tone, in_browser, sync_playwright
from spoken.models import ItemState, SpokenItem

from .support import People, assign, no_air_type, regular_type


@unittest.skipIf(sync_playwright is None or not ffmpeg_available(), "Playwright/ffmpeg not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class ProducedContentWorkstationBrowserTests(People, StaticLiveServerTestCase):
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
        self.news = regular_type("town_news", recipe_key="lcl_psa", name="Town News")
        self.practice = no_air_type("practice", name="Practice")
        assign(self.producer, self.news, self.practice)
        client = Client()
        client.force_login(self.producer)                        # an ordinary (non-staff) producer
        self.session_cookie = client.cookies["sessionid"].value

    def open(self, query):
        context = self.browser.new_context()
        context.add_cookies([{"name": "sessionid", "value": self.session_cookie, "url": self.live_server_url}])
        page = context.new_page()
        page.errors, page.requests = [], []
        page.on("pageerror", lambda exc: page.errors.append(str(exc)))
        page.on("request", lambda req: page.requests.append(req.url))
        page.goto(f"{self.live_server_url}/iportal/content/record/?{query}")
        page.wait_for_function("window.IPortalWorkstation !== undefined")
        return page

    def record_and_save(self, page):
        page.click("#ipArm")
        page.wait_for_function("window.IPortalWorkstation.recorder && "
                               "window.IPortalWorkstation.recorder.state === 'armed'")
        page.click("#ipRecord")
        page.wait_for_timeout(1500)
        page.click("#ipStop")
        page.wait_for_function("window.IPortalWorkstation.recorder.state !== 'stopping' && "
                               "window.IPortalWorkstation.pcm")
        page.click("#ipSave")

    @in_browser
    def test_a_regular_draft_is_submitted_explicitly_and_then_read_only(self):
        item = regular.start(self.producer, self.news, title="Lunch menu")
        page = self.open(f"item={item.pk}")
        self.assertEqual(page.text_content("#ipSave"), "Submit for processing")
        self.assertEqual(page.text_content("#ipEditorHint"),
                         "browser only; nothing is submitted or on air until you submit")
        self.assertIn("Nothing submitted yet.", page.text_content("#ipAirCurrent"))
        body = page.text_content("body")
        self.assertNotIn("Save & put on air", body)
        self.assertNotIn("on air until you save", body)
        self.record_and_save(page)
        page.wait_for_selector("#ipMessages .ip-banner.ok >> text=Submitted", timeout=60000)
        self.assertIn("continue automatically", page.text_content("#ipMessages"))
        self.assertNotIn("This take is now", page.text_content("#ipMessages"))
        stored = SpokenItem.objects.get(pk=item.pk)
        self.assertEqual(stored.state, ItemState.VALIDATED)                     # the worker takes it from here
        self.assertEqual(stored.submission_media.owner_id, self.producer.pk)
        self.assertEqual(Track.objects.count(), 0)                               # nothing projected in the request
        self.assertTrue(page.is_disabled("#ipSave"))
        self.assertEqual(page.errors, [])
        self.assertFalse([url for url in page.requests if "/api/engine" in url or "/api/fx" in url])

    @in_browser
    def test_a_test_take_is_saved_and_nothing_can_air(self):
        page = self.open("test_type=practice")
        self.assertEqual(page.text_content("#ipAirBadge"), "TEST / NO AIR")
        self.assertEqual(page.text_content("#ipSave"), "Save test take")
        self.assertIn("Nothing in this workspace can air.", page.text_content("#ipPurpose"))
        self.record_and_save(page)
        page.wait_for_selector("#ipMessages .ip-banner.ok >> text=Test take saved", timeout=60000)
        take = ProducedContentTestTake.objects.get()
        self.assertEqual((take.user_id, take.content_type_id), (self.producer.pk, self.practice.pk))
        self.assertEqual((SpokenItem.objects.count(), Track.objects.count()), (0, 0))
        self.assertEqual(page.errors, [])
        self.assertFalse([url for url in page.requests if "/api/engine" in url or "/api/fx" in url])
