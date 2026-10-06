"""2.22B -- evergreen VoiceTrack end to end in a real (headless, hardware-free)
browser: the talent role records in the VoiceTrack studio, saves, and the
VoiceTrack is atomically bound to the new immutable take that the engine's
resolver now plays. Same hardware-free Chromium as
production.tests.test_recorder_browser."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, override_settings

from library.models import VoiceTrack
from library.tests.test_voicetrack_production_media import make_track
from production.services import layout
from production.tests.support import IsolatedMediaRootMixin, ffmpeg_available
from production.tests.test_recorder_browser import _tone, in_browser, sync_playwright

User = get_user_model()


@unittest.skipIf(sync_playwright is None or not ffmpeg_available(), "Playwright/ffmpeg not available")
@override_settings(SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False, CSRF_COOKIE_SECURE=False)
class VoiceTrackStudioBrowserTests(IsolatedMediaRootMixin, StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.assets = tempfile.TemporaryDirectory()
        assets = Path(cls.assets.name)
        _tone(assets / "mic.wav", 30)
        (assets / "empty-alsa.conf").write_text("")
        cls.browser_env = {**os.environ, "ALSA_CONFIG_PATH": str(assets / "empty-alsa.conf"),
                           "PULSE_SERVER": "unix:/nonexistent/pulse", "PULSE_RUNTIME_PATH": str(assets)}
        cls.browser_args = [
            "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={assets / 'mic.wav'}",
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
        # A TransactionTestCase flush removes migration-seeded rows: recreate the
        # talent group's capability and path grants exactly as seeded.
        from authz.models import Capability, GroupRole, Role, RoleCapability
        from library.models import GroupAccess
        capability, _ = Capability.objects.get_or_create(slug="voicetrack.record",
                                                         defaults={"label": "Record/manage voice tracks"})
        role = Role.objects.create(name="Talent (test)")
        RoleCapability.objects.create(role=role, capability=capability)
        group, _ = Group.objects.get_or_create(name="remote_dj")
        GroupRole.objects.update_or_create(group=group, defaults={"role": role})
        GroupAccess.objects.update_or_create(group=group, defaults={
            "allowed_prefixes": "/voicetracks/\n/api/voicetrack/\n/static/", "landing_url": "/voicetracks/"})
        self.talent = User.objects.create_user("talent", password="x")
        self.talent.groups.add(group)
        self.track = make_track()
        client = Client()
        client.force_login(self.talent)
        self.session_cookie = client.cookies["sessionid"].value

    @in_browser
    def test_talent_records_saves_and_the_engine_resolver_plays_the_new_take(self):
        context = self.browser.new_context()
        context.add_cookies([{"name": "sessionid", "value": self.session_cookie, "url": self.live_server_url}])
        page = context.new_page()
        errors, requests = [], []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("request", lambda req: requests.append(req.url))
        page.goto(f"{self.live_server_url}/voicetracks/studio/?track={self.track.pk}&position=intro")
        page.wait_for_function("window.IPortalWorkstation !== undefined")
        self.assertIn("Intro voice track", page.text_content("#ipTitle"))
        self.assertIn("Nothing on air yet", page.text_content("#ipAirCurrent"))
        page.click("#ipArm")
        page.wait_for_function("window.IPortalWorkstation.recorder.state === 'armed'")
        page.click("#ipRecord")
        page.wait_for_timeout(1500)
        page.click("#ipStop")
        page.wait_for_function("window.IPortalWorkstation.pcm")
        page.click("#ipSave")
        page.wait_for_selector("#ipMessages .ip-banner.ok >> text=Saved", timeout=60000)
        self.assertIn("On-air take (iPortal)", page.text_content("#ipAirCurrent"))

        vt = VoiceTrack.objects.select_related("media").get(track=self.track, position="intro")
        self.assertEqual((vt.recorded_by, vt.filepath, vt.media.kind), (self.talent, "", "recording"))
        audio = vt.playable_audio()
        self.assertEqual(audio.origin, "production_media")
        self.assertEqual(audio.path, str(layout.resolve_storage_path(vt.media.storage_key)))
        self.assertAlmostEqual(audio.duration_seconds, 1.5, delta=0.4)
        self.assertEqual(errors, [])
        self.assertFalse([url for url in requests if "/api/engine" in url], "preview/record must never touch air")
