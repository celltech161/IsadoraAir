"""r0113 / D3C: an evergreen VoiceTrack never binds NO-AIR (TEST) media --
the take, nor any edit derived from it -- so no VoiceTrack can play it."""
import io

from django.contrib.auth import get_user_model
from django.test import TestCase

from library.models import VoiceTrack
from library.services import voicetrack_media as vtm
from production.services import intake
from production.tests.support import IsolatedMediaRootMixin, fixture

from .test_voicetrack_production_media import ingest_take, make_track

User = get_user_model()


def no_air_take(user, parent=None):
    source = io.BytesIO(fixture("wav16_mono.wav"))
    if parent is not None:
        return intake.ingest_derivative(parent, source, owner=user, policy=vtm.VOICETRACK_POLICY,
                                        recipe_key="iportal.editor", recipe_version=1,
                                        recipe_params={"ops": ["trim"]}).media
    return intake.ingest_stream(source, kind="recording", owner=user, policy=vtm.VOICETRACK_POLICY,
                                airing_prohibited=True).media


class VoiceTrackNoAirTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("host", password="x")
        self.track = make_track()

    def bind(self, media):
        return vtm.bind_media(track_id=self.track.pk, position="intro", media_id=media.pk, user=self.user,
                              expected_revision=vtm.ABSENT)

    def test_no_air_media_and_its_edits_are_refused_and_nothing_becomes_playable(self):
        take = no_air_take(self.user)
        edit = no_air_take(self.user, parent=take)                 # an edit made in the VoiceTrack workspace
        self.assertTrue(edit.airing_prohibited)
        for media in (take, edit):
            with self.subTest(kind=media.kind), self.assertRaises(vtm.VoiceTrackMediaRefused) as raised:
                self.bind(media)
            self.assertEqual(raised.exception.code, "airing_prohibited")
        self.assertFalse(VoiceTrack.objects.filter(track=self.track).exists())
        self.assertIsNone(vtm.current(self.track.pk, "intro"))

    def test_ordinary_takes_still_bind(self):
        voicetrack = self.bind(ingest_take(self.user))
        self.assertEqual(voicetrack.playable_audio().origin, "production_media")
