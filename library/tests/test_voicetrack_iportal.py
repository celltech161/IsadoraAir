"""2.22B B24 / VoiceTrack compatibility matrix through the REAL routes.

The evergreen VoiceTrack workspace is the shared recorder mounted under the
existing voice-track URL space, authorized by the existing ``voicetrack.record``
capability and reachable by the seeded remote_dj talent role with no new
path grants.
"""
import json
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings

from library.models import VoiceTrack
from library.services import voicetrack_media as vtm
from library.tests.test_voicetrack_production_media import make_legacy_vt, make_track
from production.models import ProductionMedia
from production.services import layout, retention
from production.tests.support import IsolatedMediaRootMixin, fixture

User = get_user_model()
STUDIO = "/voicetracks/studio/"
API = "/api/voicetrack/iportal/"


@override_settings(SECURE_SSL_REDIRECT=False)
class EvergreenVoiceTrackWorkspaceTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.track = make_track()
        self.talent = User.objects.create_user("talent", password="x")
        self.talent.groups.add(Group.objects.get(name="remote_dj"))
        self.host = User.objects.create_user("host", password="x", is_staff=True)
        self.listener = User.objects.create_user("listener", password="x")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def session(self, user):
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        page = client.get(f"{STUDIO}?track={self.track.pk}&position=intro")
        client.csrf = client.cookies["csrftoken"].value if "csrftoken" in client.cookies else ""
        client.page = page
        return client

    def subject(self, position="intro"):
        return {"track": self.track.pk, "position": position}

    def context(self, client, position="intro"):
        return client.get(f"{API}context/?track={self.track.pk}&position={position}").json()["context"]

    def take(self, client, name="wav16_mono.wav", mode="record", extra="", position="intro", ctype="audio/wav"):
        return client.generic("POST", f"{API}take/?track={self.track.pk}&position={position}&mode={mode}{extra}",
                              fixture(name), content_type=ctype, HTTP_X_CSRFTOKEN=client.csrf)

    def post(self, client, endpoint, data):
        return client.post(f"{API}{endpoint}/", json.dumps(data), content_type="application/json",
                           HTTP_X_CSRFTOKEN=client.csrf)

    def save(self, client, revision, **kwargs):
        media_id = self.take(client, **kwargs).json()["media"]["media_id"]
        response = self.post(client, "commit", {"subject": self.subject(kwargs.get("position", "intro")),
                                                "media_id": media_id, "revision": revision})
        return media_id, response

    # -- access -----------------------------------------------------------------
    def test_the_talent_role_opens_the_studio_with_the_existing_capability_and_paths(self):
        client = self.session(self.talent)
        self.assertEqual(client.page.status_code, 200)
        self.assertContains(client.page, "Intro voice track")
        self.assertContains(client.page, "Intro ends at")

    def test_users_without_the_capability_are_refused_server_side(self):
        client = self.session(self.listener)
        self.assertNotEqual(client.page.status_code, 200)
        response = client.generic("POST", f"{API}take/?track={self.track.pk}&position=intro&mode=record",
                                  fixture("wav16_mono.wav"), content_type="audio/wav")
        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(ProductionMedia.objects.exists())

    def test_csrf_is_enforced_on_every_write(self):
        client = self.session(self.talent)
        bare = Client(enforce_csrf_checks=True)
        bare.force_login(self.talent)
        self.assertEqual(bare.generic("POST", f"{API}take/?track={self.track.pk}&position=intro&mode=record",
                                      fixture("wav16_mono.wav"), content_type="audio/wav").status_code, 403)
        self.assertEqual(bare.post(f"{API}commit/", "{}", content_type="application/json").status_code, 403)
        self.assertEqual(bare.post(f"{API}remove/", "{}", content_type="application/json").status_code, 403)
        self.assertFalse(VoiceTrack.objects.exists())
        self.assertEqual(client.page.status_code, 200)

    def test_the_destructive_pre_phase_b_endpoints_are_retired(self):
        client = self.session(self.host)
        for url in ("/api/voicetrack/upload/", "/api/voicetrack/1/save-edited/", "/api/voicetrack/1/delete/"):
            with self.subTest(url=url):
                self.assertEqual(client.post(url, HTTP_X_CSRFTOKEN=client.csrf).status_code, 404)

    # -- new ProductionMedia VoiceTrack: record, validate, save, bind, preview --------
    def test_record_save_bind_preview_and_reopen(self):
        client = self.session(self.talent)
        media_id, response = self.save(client, vtm.ABSENT)
        self.assertEqual(response.status_code, 200, response.content)
        vt = VoiceTrack.objects.get(track=self.track, position="intro")
        self.assertEqual((str(vt.media_id), vt.filepath, vt.recorded_by), (media_id, "", self.talent))
        ctx = response.json()["context"]
        self.assertEqual(ctx["current"]["origin"], "production_media")
        self.assertNotEqual(ctx["revision"], vtm.ABSENT)
        preview = client.get(ctx["current"]["preview_url"])
        self.assertEqual(b"".join(preview.streaming_content)[:4], b"RIFF")
        legacy_preview = client.get(f"/api/voicetrack/{vt.pk}/audio/")      # the track page's player
        self.assertEqual(b"".join(legacy_preview.streaming_content)[:4], b"RIFF")
        reopened = self.session(self.talent)
        self.assertContains(reopened.page, "On-air take (iPortal)")

    def test_rerecord_repoints_atomically_and_keeps_the_old_take(self):
        client = self.session(self.talent)
        first, _ = self.save(client, vtm.ABSENT)
        vt_pk = VoiceTrack.objects.get(track=self.track, position="intro").pk
        second, response = self.save(client, self.context(client)["revision"], name="flac.flac", ctype="audio/flac")
        self.assertEqual(response.status_code, 200)
        vt = VoiceTrack.objects.get(track=self.track, position="intro")
        self.assertEqual((vt.pk, str(vt.media_id)), (vt_pk, second))
        old = ProductionMedia.objects.get(pk=first)
        self.assertTrue(old.is_present and old.is_valid)
        self.assertEqual(retention.find_references(old), [])

    def test_editing_the_on_air_take_creates_a_derivative_and_rebinds(self):
        client = self.session(self.talent)
        original, _ = self.save(client, vtm.ABSENT)
        source = client.get(f"{API}source/?track={self.track.pk}&position=intro")
        self.assertEqual(b"".join(source.streaming_content)[:4], b"RIFF")
        ops = json.dumps(["trim-keep", "normalize"])
        edit = self.take(client, mode="edit", extra=f"&derived_from={original}&operations={ops}")
        self.assertEqual(edit.status_code, 201, edit.content)
        edit_id = edit.json()["media"]["media_id"]
        response = self.post(client, "commit", {"subject": self.subject(), "media_id": edit_id,
                                                "revision": self.context(client)["revision"]})
        self.assertEqual(response.status_code, 200)
        child = ProductionMedia.objects.get(pk=edit_id)
        self.assertEqual((child.kind, str(child.derived_from_id)), ("edit", original))
        self.assertEqual(str(VoiceTrack.objects.get(track=self.track, position="intro").media_id), edit_id)

    def test_a_legacy_voicetrack_converts_through_the_normal_save_path(self):
        legacy = Path(self.tmp.name) / "legacy.wav"
        legacy.write_bytes(fixture("wav16_mono.wav"))
        vt = make_legacy_vt(self.track, path=str(legacy))
        client = self.session(self.talent)
        ctx = self.context(client)
        self.assertEqual(ctx["current"]["origin"], "legacy")
        source = client.get(f"{API}source/?track={self.track.pk}&position=intro")
        self.assertEqual(b"".join(source.streaming_content), legacy.read_bytes())
        media_id, response = self.save(client, ctx["revision"], mode="import")
        self.assertEqual(response.status_code, 200, response.content)
        converted = VoiceTrack.objects.get(pk=vt.pk)                     # same identity
        self.assertEqual((str(converted.media_id), converted.source), (media_id, "import"))
        self.assertTrue(legacy.exists())                                  # legacy file untouched
        self.assertEqual(converted.playable_audio().origin, "production_media")

    def test_viewing_a_legacy_voicetrack_never_migrates_it(self):
        legacy = Path(self.tmp.name) / "legacy.wav"
        legacy.write_bytes(fixture("wav16_mono.wav"))
        vt = make_legacy_vt(self.track, path=str(legacy))
        client = self.session(self.talent)
        self.context(client)
        client.get(f"{API}source/?track={self.track.pk}&position=intro")
        client.get(f"/api/voicetrack/{vt.pk}/audio/")
        self.assertIsNone(VoiceTrack.objects.get(pk=vt.pk).media_id)
        self.assertFalse(ProductionMedia.objects.exists())

    # -- concurrency / failure ---------------------------------------------------------
    def test_a_stale_browser_cannot_overwrite_a_newer_binding(self):
        talent, host = self.session(self.talent), self.session(self.host)
        opened = self.context(talent)["revision"]
        self.assertEqual(opened, self.context(host)["revision"])
        winner, ok = self.save(host, opened, name="flac.flac", ctype="audio/flac")
        self.assertEqual(ok.status_code, 200)
        loser, stale = self.save(talent, opened)
        self.assertEqual((stale.status_code, stale.json()["error"]), (409, "stale_revision"))
        self.assertEqual(str(VoiceTrack.objects.get(track=self.track, position="intro").media_id), winner)
        kept = ProductionMedia.objects.get(pk=loser)
        self.assertTrue(kept.is_present)                                    # the late take is kept, unbound
        self.assertEqual(retention.find_references(kept), [])

    def test_failed_validation_never_changes_the_airable_binding(self):
        client = self.session(self.talent)
        good, _ = self.save(client, vtm.ABSENT)
        bad = client.generic("POST", f"{API}take/?track={self.track.pk}&position=intro&mode=import",
                             b"\x00" * 8192, content_type="application/octet-stream", HTTP_X_CSRFTOKEN=client.csrf)
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(str(VoiceTrack.objects.get(track=self.track, position="intro").media_id), good)

    def test_a_track_without_markers_cannot_get_a_voicetrack(self):
        bare = make_track("Bare", intro=None, outro=None)
        client = self.session(self.talent)
        ctx = client.get(f"{API}context/?track={bare.pk}&position=intro").json()["context"]
        self.assertIn("intro_until", ctx["blocked_reason"])
        media_id = client.generic("POST", f"{API}take/?track={bare.pk}&position=intro&mode=record",
                                  fixture("wav16_mono.wav"), content_type="audio/wav",
                                  HTTP_X_CSRFTOKEN=client.csrf).json()["media"]["media_id"]
        response = self.post(client, "commit", {"subject": {"track": bare.pk, "position": "intro"},
                                                "media_id": media_id, "revision": vtm.ABSENT})
        self.assertEqual((response.status_code, response.json()["error"]), (400, "marker_missing"))
        self.assertFalse(VoiceTrack.objects.filter(track=bare).exists())

    # -- delete ---------------------------------------------------------------------------
    def test_remove_from_air_deletes_the_row_and_keeps_the_media(self):
        client = self.session(self.talent)
        media_id, _ = self.save(client, vtm.ABSENT)
        response = self.post(client, "remove", {"subject": self.subject(), "revision": self.context(client)["revision"]})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(VoiceTrack.objects.filter(track=self.track).exists())
        media = ProductionMedia.objects.get(pk=media_id)
        self.assertTrue(media.is_present)
        self.assertTrue(layout.resolve_storage_path(media.storage_key).is_file())
        self.assertIsNone(response.json()["context"]["current"])

    # -- media authorization -----------------------------------------------------------------
    def test_media_ids_are_authorization_checked(self):
        host, talent = self.session(self.host), self.session(self.talent)
        unbound = self.take(host).json()["media"]["media_id"]
        self.assertEqual(talent.get(f"{API}media/{unbound}/?track={self.track.pk}&position=intro").status_code, 404)
        self.assertEqual(self.post(talent, "commit", {"subject": self.subject(), "media_id": unbound,
                                                      "revision": vtm.ABSENT}).status_code, 404)
        bound, _ = self.save(host, vtm.ABSENT)
        self.assertEqual(talent.get(f"{API}media/{bound}/?track={self.track.pk}&position=intro").status_code, 200)
        # ...but only in the context of the subject it is bound to.
        other = make_track("Other")
        self.assertEqual(talent.get(f"{API}media/{bound}/?track={other.pk}&position=intro").status_code, 404)

    # -- pages -----------------------------------------------------------------------------
    def test_track_and_index_pages_link_to_the_studio_and_show_the_source(self):
        client = self.session(self.talent)
        self.save(client, vtm.ABSENT)
        detail = client.get(f"/track/{self.track.pk}/")
        self.assertContains(detail, "Re-record / edit in iPortal")
        self.assertContains(detail, "iPortal take")
        self.assertNotContains(detail, "api/voicetrack/upload")
        index = client.get("/voicetracks/")
        self.assertContains(index, "Open in iPortal")
        self.assertContains(index, "iPortal take")
