"""2.22B -- evergreen VoiceTrack end to end in a real (headless, hardware-free)
browser: the talent role records in the VoiceTrack studio, saves, and the
VoiceTrack is atomically bound to the new immutable take that the engine's
resolver now plays. Same hardware-free Chromium as
production.tests.test_recorder_browser."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client, override_settings

from library.models import VoiceTrack
from library.services import voicetrack_media as vtm
from library.tests.test_voicetrack_production_media import ingest_take, make_track
from production.models import ProductionMedia
from production.services import retention
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
        self.other_talent = User.objects.create_user("talent-b", password="x")
        self.other_talent.groups.add(group)
        self.track = make_track()
        self.session_cookie = self.login(self.talent)

    @staticmethod
    def login(user):
        client = Client()
        client.force_login(user)
        return client.cookies["sessionid"].value

    # -- harness -----------------------------------------------------------------
    def as_user(self, context, cookie):
        """Switch the SAME browser profile (same IndexedDB) to another session."""
        context.clear_cookies()
        context.add_cookies([{"name": "sessionid", "value": cookie, "url": self.live_server_url}])

    def studio(self, page, position="intro"):
        page.goto(f"{self.live_server_url}/voicetracks/studio/?track={self.track.pk}&position={position}")
        page.wait_for_function("window.IPortalWorkstation !== undefined")
        page.wait_for_timeout(800)                 # pruneDrafts().then(checkDraft) settles

    def record(self, page, seconds):
        page.click("#ipArm")
        page.wait_for_function("window.IPortalWorkstation.recorder.state === 'armed'")
        page.click("#ipRecord")
        page.wait_for_timeout(int(seconds * 1000))
        page.click("#ipStop")
        page.wait_for_function("window.IPortalWorkstation.recorder.state !== 'stopping' && window.IPortalWorkstation.pcm")

    DRAFT_KEYS = """() => new Promise((resolve, reject) => {
        const open = indexedDB.open("iportal-drafts", 1);
        open.onupgradeneeded = () => open.result.createObjectStore("drafts");
        open.onerror = () => reject(open.error);
        open.onsuccess = () => {
            const db = open.result, req = db.transaction("drafts").objectStore("drafts").getAllKeys();
            req.onsuccess = () => { resolve(req.result.map(String)); db.close(); };
        };
    })"""

    def draft_keys(self, page):
        return page.evaluate(self.DRAFT_KEYS)

    def wait_for_draft(self, page, count):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if len(self.draft_keys(page)) >= count:
                return
            page.wait_for_timeout(200)
        self.fail(f"no draft saved (keys: {self.draft_keys(page)})")

    def banner_shown(self, page):
        return page.evaluate("!document.getElementById('ipDraftBanner').hidden")

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

    # -- r0108: reopening with a take already on air ----------------------------------

    @in_browser
    def test_reopening_shows_the_on_air_take_and_creates_nothing(self):
        media = ingest_take()
        bound = vtm.bind_media(track_id=self.track.pk, position="intro", media_id=media.pk,
                               user=self.other_talent, expected_revision=vtm.ABSENT)
        revision = vtm.revision_of(bound)
        takes = set(ProductionMedia.objects.values_list("pk", flat=True))
        context = self.browser.new_context()
        self.as_user(context, self.session_cookie)
        page = context.new_page()
        errors, writes = [], []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("request", lambda req: writes.append(req.url) if req.method != "GET" else None)
        self.studio(page)

        # The editor says a take is on air and offers it; nothing is loaded by itself.
        self.assertFalse(page.evaluate("document.getElementById('ipEditorEmpty').hidden"))
        self.assertIn("already on air", page.text_content("#ipEditorEmpty"))
        self.assertEqual(page.text_content("#ipSelection"), "Editor empty — the on-air take is not loaded.")
        self.assertIsNone(page.evaluate("IPortalWorkstation.pcm"))
        self.assertEqual(page.evaluate("IPortalWorkstation.sourceKind"), "none")
        self.assertTrue(page.is_disabled("#ipSave"))

        page.click("#ipLoadCurrentEmpty")
        page.wait_for_function("window.IPortalWorkstation.pcm")
        self.assertEqual(page.evaluate("[IPortalWorkstation.sourceKind, IPortalWorkstation.parentMediaId, "
                                       "IPortalWorkstation.dirty, IPortalWorkstation.baseRevision]"),
                         ["edit-media", str(media.pk), False, revision])
        self.assertTrue(page.evaluate("document.getElementById('ipEditorEmpty').hidden"))
        self.assertTrue(page.is_disabled("#ipSave"))                       # an unedited load is not a change
        page.wait_for_timeout(1500)                                          # past the draft debounce

        # Opening and loading created, promoted and drafted nothing.
        self.assertEqual(writes, [])
        self.assertEqual(self.draft_keys(page), [])
        self.assertEqual(set(ProductionMedia.objects.values_list("pk", flat=True)), takes)
        row = VoiceTrack.objects.get(track=self.track, position="intro")
        self.assertEqual((row.media_id, vtm.revision_of(row)), (media.pk, revision))
        self.assertEqual(errors, [])

    @in_browser
    def test_with_nothing_on_air_the_editor_is_simply_empty(self):
        context = self.browser.new_context()
        self.as_user(context, self.session_cookie)
        page = context.new_page()
        self.studio(page)
        self.assertTrue(page.evaluate("document.getElementById('ipEditorEmpty').hidden"))
        self.assertEqual(page.text_content("#ipSelection"), "No audio yet.")

    # -- 2.22B corrective: drafts keep their revision, and belong to one user --------

    @in_browser
    def test_a_restored_stale_draft_conflicts_instead_of_overwriting_a_newer_save(self):
        """Codex: a restored draft used the page's FRESH revision, so an old draft
        silently overwrote another editor's newer save."""
        first = vtm.bind_media(track_id=self.track.pk, position="intro", media_id=ingest_take().pk,
                               user=self.other_talent, expected_revision=vtm.ABSENT)
        revision_n = vtm.revision_of(first)
        context = self.browser.new_context()
        self.as_user(context, self.session_cookie)
        page = context.new_page()
        errors, commits = [], []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        page.on("request", lambda req: commits.append(json.loads(req.post_data))
                if req.url.endswith("/commit/") else None)
        # 1. editor A starts an unsaved draft against revision N
        self.studio(page)
        self.assertEqual(page.evaluate("IPortalWorkstation.ctx.revision"), revision_n)
        self.record(page, 1.5)
        self.wait_for_draft(page, 1)
        # 2. editor B saves: revision N+1
        newer_media = ingest_take(name="flac.flac")
        newer = vtm.bind_media(track_id=self.track.pk, position="intro", media_id=newer_media.pk,
                               user=self.other_talent, expected_revision=revision_n)
        revision_n1 = vtm.revision_of(newer)
        self.assertNotEqual(revision_n1, revision_n)
        # 3./4. A reloads and restores the revision-N draft (the page itself is at N+1)
        page.reload()
        page.wait_for_function("window.IPortalWorkstation !== undefined")
        page.wait_for_selector("#ipDraftBanner:not([hidden])")
        self.assertIn("changed since then", page.text_content("#ipDraftText"))     # the stale warning stays
        page.click("#ipDraftRestore")
        self.assertEqual(page.evaluate("IPortalWorkstation.ctx.revision"), revision_n1)
        self.assertEqual(page.evaluate("IPortalWorkstation.baseRevision"), revision_n)
        takes_before = set(ProductionMedia.objects.filter(owner=self.talent).values_list("pk", flat=True))
        # 5./6. saving submits revision N and gets the existing conflict response
        page.click("#ipSave")
        page.wait_for_selector("#ipConflict:not([hidden])", timeout=60000)
        self.assertEqual([body["revision"] for body in commits], [revision_n])
        # 7. revision N+1 is still bound
        row = VoiceTrack.objects.get(track=self.track, position="intro")
        self.assertEqual((row.media_id, vtm.revision_of(row)), (newer_media.pk, revision_n1))
        # 8. A's uploaded take exists, unbound and reclaimable
        uploaded = ProductionMedia.objects.filter(owner=self.talent).exclude(pk__in=takes_before)
        self.assertEqual(uploaded.count(), 1)
        self.assertEqual(retention.find_references(uploaded.get()), [])
        self.assertEqual(errors, [])

    @in_browser
    def test_drafts_are_private_to_each_user_on_a_shared_browser_profile(self):
        context = self.browser.new_context()           # ONE profile: one IndexedDB for everyone
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        # A pre-namespacing draft (any user's) is pruned, never offered.
        self.as_user(context, self.session_cookie)
        self.studio(page)
        page.evaluate("""() => new Promise((resolve) => {
            const open = indexedDB.open("iportal-drafts", 1);
            open.onsuccess = () => {
                const db = open.result, tx = db.transaction("drafts", "readwrite");
                tx.objectStore("drafts").put({savedAt: Date.now(), revision: "absent", sourceKind: "recording",
                    ops: [], sampleRate: 48000, channels: [new Float32Array(48000)]},
                    "evergreen-voicetrack|" + JSON.stringify(IPortalWorkstation.ctx.subject));
                tx.oncomplete = () => { db.close(); resolve(); };
            };
        })""")
        self.studio(page)
        self.assertFalse(self.banner_shown(page))
        self.assertEqual(self.draft_keys(page), [])
        # 1. user A creates a draft for the intro
        self.record(page, 1.5)
        self.wait_for_draft(page, 1)
        a_keys = self.draft_keys(page)
        # 2.-5. A logs out, B logs in on the same profile and opens the same subject: nothing offered
        self.as_user(context, self.login(self.other_talent))
        self.studio(page)
        self.assertFalse(self.banner_shown(page))
        self.assertFalse(page.evaluate("Boolean(IPortalWorkstation.draft)"))
        self.assertNotIn(page.evaluate("IPortalWorkstation.page.draft_namespace"), a_keys[0])
        self.assertEqual(self.draft_keys(page), a_keys)                   # ... and A's draft is untouched
        self.record(page, 3.0)                                             # B's own draft, separate key
        self.wait_for_draft(page, 2)
        # 6. A logs back in and gets A's own draft back -- not B's
        self.as_user(context, self.login(self.talent))
        self.studio(page)
        self.assertTrue(self.banner_shown(page))
        page.click("#ipDraftRestore")
        self.assertAlmostEqual(page.evaluate("IPortalAudio.duration(IPortalWorkstation.pcm)"), 1.5, delta=0.4)
        # Different subjects of one user never collide: the outro has no draft.
        self.studio(page, "outro")
        self.assertFalse(self.banner_shown(page))
        self.studio(page, "intro")
        self.assertTrue(self.banner_shown(page))
        self.assertEqual(len(set(self.draft_keys(page))), 2)
        self.assertEqual(errors, [])
