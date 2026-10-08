"""r0108: the browser preview of an evergreen VoiceTrack always plays the take
that is CURRENTLY bound.

r0107 on KOGR: after a re-record the Track page's player replayed the previous
take -- it used one fixed logical URL (/api/voicetrack/<pk>/audio/) for every
take the row ever had, and a browser can keep audio it already loaded under a
URL. Pages now link the preview as ``?take=<the bound take's identity>``: a
rebinding changes the URL, an unchanged binding keeps it stable, a superseded
URL redirects to the current take (after the access check), and the logical
endpoint is never cached. The on-air resolution (playable_audio) is
unchanged.
"""
import re
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings

from library.models import VoiceTrack
from library.services import voicetrack_media as vtm
from library.tests import test_voicetrack_iportal as workspace_tests
from library.tests.test_voicetrack_production_media import make_legacy_vt, make_track
from production.models import ProductionMedia
from production.services import layout
from production.tests.support import IsolatedMediaRootMixin, fixture

User = get_user_model()
PLAYER = re.compile(r'<audio[^>]*data-vt-preview[^>]*src="([^"]+)"')


def body(response) -> bytes:
    return b"".join(response.streaming_content) if response.streaming else response.content


@override_settings(SECURE_SSL_REDIRECT=False)
class VoiceTrackPreviewCacheTests(IsolatedMediaRootMixin, TestCase):
    # The workspace tests' own request helpers (referenced through the module
    # so the runner does not collect that test class a second time here).
    _workspace = workspace_tests.EvergreenVoiceTrackWorkspaceTests
    session, subject, context, take, post, save = (
        _workspace.session, _workspace.subject, _workspace.context, _workspace.take, _workspace.post,
        _workspace.save)

    def setUp(self):
        super().setUp()
        self.track = make_track()
        self.host = User.objects.create_user("host", password="x", is_staff=True)
        self.talent = User.objects.create_user("talent", password="x")
        self.talent.groups.add(Group.objects.get(name="remote_dj"))
        self.listener = User.objects.create_user("listener", password="x")
        self.studio = self.session(self.host)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    # -- helpers ---------------------------------------------------------------------
    def record(self, name="wav16_mono.wav", ctype="audio/wav", mode="record"):
        media_id, response = self.save(self.studio, self.context(self.studio)["revision"], name=name, ctype=ctype,
                                       mode=mode)
        self.assertEqual(response.status_code, 200, response.content)
        return media_id

    def track_page_player(self):
        page = self.studio.get(f"/track/{self.track.pk}/")
        self.assertEqual(page.status_code, 200)
        players = PLAYER.findall(page.content.decode())
        self.assertEqual(len(players), 1, players)
        return players[0].replace("&amp;", "&")

    def media_bytes(self, media_id) -> bytes:
        return layout.resolve_storage_path(ProductionMedia.objects.get(pk=media_id).storage_key).read_bytes()

    def assert_plays(self, url, media_id, client=None):
        response = (client or self.studio).get(url)
        self.assertEqual(response.status_code, 200, url)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(body(response), self.media_bytes(media_id))

    def assert_redirects_to_current(self, stale_url, current_url):
        response = self.studio.get(stale_url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], current_url)
        self.assertEqual(response["Cache-Control"], "private, no-store")

    # -- binding changes ------------------------------------------------------------------
    def test_first_replacement_and_repeated_replacement_each_get_their_own_url(self):
        first = self.record()
        vt = VoiceTrack.objects.get(track=self.track, position="intro")
        url_1 = self.track_page_player()
        self.assertEqual(url_1, f"/api/voicetrack/{vt.pk}/audio/?take={first}")
        self.assert_plays(url_1, first)

        second = self.record(name="flac.flac", ctype="audio/flac")
        url_2 = self.track_page_player()
        self.assertNotEqual(url_2, url_1)
        self.assertEqual(url_2, f"/api/voicetrack/{vt.pk}/audio/?take={second}")
        self.assert_plays(url_2, second)
        self.assertNotEqual(self.media_bytes(first), self.media_bytes(second))
        self.assert_redirects_to_current(url_1, url_2)            # a page rendered before the re-record

        third = self.record(name="wav24_stereo.wav")
        url_3 = self.track_page_player()
        self.assertEqual(len({url_1, url_2, url_3}), 3)
        self.assert_plays(url_3, third)
        for stale in (url_1, url_2):
            self.assert_redirects_to_current(stale, url_3)
        # Following a stale link lands on the CURRENT take's bytes.
        followed = self.studio.get(url_1, follow=True)
        self.assertEqual(body(followed), self.media_bytes(third))
        # Old takes are retained (never deleted to make the preview right).
        self.assertEqual(ProductionMedia.objects.filter(pk__in=[first, second, third]).count(), 3)

    def test_an_unchanged_binding_keeps_a_stable_url(self):
        media = self.record()
        self.assertEqual(self.track_page_player(), self.track_page_player())
        url = self.track_page_player()
        self.assert_plays(url, media)
        self.assert_plays(url, media)

    def test_delete_and_recreate(self):
        self.record()
        old_vt = VoiceTrack.objects.get(track=self.track, position="intro")
        old_url = self.track_page_player()
        removed = self.post(self.studio, "remove", {"subject": self.subject(),
                                                    "revision": self.context(self.studio)["revision"]})
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(self.studio.get(old_url).status_code, 404)
        recreated = self.record(name="flac.flac", ctype="audio/flac")
        new_vt = VoiceTrack.objects.get(track=self.track, position="intro")
        self.assertNotEqual(new_vt.pk, old_vt.pk)
        new_url = self.track_page_player()
        self.assertNotEqual(new_url, old_url)
        self.assert_plays(new_url, recreated)
        self.assertEqual(self.studio.get(old_url).status_code, 404)       # the old row stays gone

    def test_range_requests_still_work_on_the_versioned_url(self):
        media = self.record()
        url = self.track_page_player()
        partial = self.studio.get(url, HTTP_RANGE="bytes=0-3")
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(body(partial), b"RIFF")
        self.assertEqual(partial["Content-Range"], f"bytes 0-3/{len(self.media_bytes(media))}")
        self.assertEqual(partial["Cache-Control"], "private, no-store")

    def test_without_a_take_parameter_the_current_take_is_served(self):
        self.record()
        current = self.record(name="flac.flac", ctype="audio/flac")
        vt = VoiceTrack.objects.get(track=self.track, position="intro")
        self.assert_plays(f"/api/voicetrack/{vt.pk}/audio/", current)

    def test_the_studio_and_the_voicetracks_list_use_the_take_identity(self):
        first = self.record()
        listing = self.studio.get("/voicetracks/")
        self.assertEqual([url.replace("&amp;", "&") for url in PLAYER.findall(listing.content.decode())],
                         [VoiceTrack.objects.get().preview_url])
        self.assertIn(f"take={first}", VoiceTrack.objects.get().preview_url)
        # The studio's own on-air player is the immutable per-take endpoint.
        self.assertIn(f"/media/{first}/", self.context(self.studio)["current"]["preview_url"])
        for page in (listing, self.studio.get(f"/track/{self.track.pk}/")):
            self.assertContains(page, 'addEventListener("pageshow"')

    # -- authorization -------------------------------------------------------------------
    def test_the_take_parameter_never_grants_access(self):
        mine = self.record()
        vt = VoiceTrack.objects.get(track=self.track, position="intro")
        other_track = make_track("Other")
        # A take bound elsewhere (another track's VT) -- known by id.
        self.track, saved = other_track, self.track
        foreign = self.record(name="flac.flac", ctype="audio/flac")
        self.track = saved

        outsider = Client()
        outsider.force_login(self.listener)
        for url in (vt.preview_url, f"/api/voicetrack/{vt.pk}/audio/?take={foreign}",
                    f"/api/voicetrack/{vt.pk}/audio/?take=legacy"):
            with self.subTest(url=url):
                response = outsider.get(url)
                self.assertNotIn(response.status_code, (200, 206, 302))
                self.assertNotIn("Location", response)
        stale = f"/api/voicetrack/{vt.pk}/audio/?take={foreign}"
        anonymous = Client().get(stale)
        self.assertEqual(anonymous.status_code, 302)                      # to the login page, which only
        self.assertTrue(anonymous["Location"].startswith("/login/"))      # echoes the requested URL back
        self.assertNotIn(str(mine), anonymous["Location"])                 # (never the current take)
        # An authorized user naming another row's take still only ever gets
        # THIS row's current take.
        response = self.studio.get(f"/api/voicetrack/{vt.pk}/audio/?take={foreign}", follow=True)
        self.assertEqual(body(response), self.media_bytes(mine))

    def test_a_remote_dj_with_the_capability_previews_through_the_versioned_url(self):
        media = self.record()
        talent = Client()
        talent.force_login(self.talent)
        self.assert_plays(VoiceTrack.objects.get().preview_url, media, client=talent)

    # -- legacy compatibility --------------------------------------------------------------
    def test_a_legacy_voicetrack_previews_and_converts(self):
        legacy = Path(self.tmp.name) / "legacy.wav"
        legacy.write_bytes(fixture("wav16_mono.wav"))
        vt = make_legacy_vt(self.track, path=str(legacy))
        url = self.track_page_player()
        self.assertEqual(url, f"/api/voicetrack/{vt.pk}/audio/?take=legacy")
        response = self.studio.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(body(response), legacy.read_bytes())
        self.assertEqual(self.context(self.studio)["current"]["preview_url"], url)
        self.assertIsNone(VoiceTrack.objects.get(pk=vt.pk).media_id)        # viewing never migrates
        # Converting it through the studio changes the identity; the legacy URL
        # then leads to the new take.
        converted = self.record(mode="import")
        new_url = self.track_page_player()
        self.assertEqual(new_url, f"/api/voicetrack/{vt.pk}/audio/?take={converted}")
        self.assert_redirects_to_current(url, new_url)
        self.assert_plays(new_url, converted)
        self.assertTrue(legacy.exists())

    def test_the_engine_resolution_is_unchanged(self):
        media = self.record()
        vt = VoiceTrack.objects.get()
        audio = vt.playable_audio()
        self.assertEqual((audio.origin, audio.path), ("production_media", str(
            layout.resolve_storage_path(ProductionMedia.objects.get(pk=media).storage_key))))
        self.assertEqual(vtm.current(self.track.pk, "intro").pk, vt.pk)
