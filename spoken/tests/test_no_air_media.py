"""r0113 / D3C: NO-AIR (TEST) media can never become spoken submission or
rendition evidence -- at the lowest spoken API, not only through the
produced-content browser path, for the owner and for a manager alike."""
from types import SimpleNamespace

from django.db import transaction

from production.models import ProductionMedia
from spoken import delivery, transitions
from spoken.models import ItemState, SpokenItem

from .test_domain import Fixtures, media

S = ItemState


class NoAirSubmissionTests(Fixtures):
    def test_the_owner_cannot_submit_no_air_media(self):
        draft = self.item()
        with self.assertRaises(transitions.EvidenceRefused) as raised:
            transitions.submit(draft, media(airing_prohibited=True), actor=self.owner)
        self.assertEqual(raised.exception.code, "airing_prohibited")
        row = SpokenItem.objects.get(pk=draft.pk)
        self.assertEqual((row.state, row.submission_media_id), (S.DRAFT, None))
        self.assertEqual(list(row.events.values_list("event", flat=True)), ["created"])

    def test_a_manager_cannot_submit_anyones_no_air_media_either(self):
        draft = self.item(actor=self.manager)
        theirs = media(airing_prohibited=True, owner=self.other)
        with self.assertRaises(transitions.EvidenceRefused) as raised:
            transitions.submit(draft, theirs, actor=self.manager)
        self.assertEqual(raised.exception.code, "airing_prohibited")
        self.assertEqual(SpokenItem.objects.get(pk=draft.pk).state, S.DRAFT)

    def test_a_derivative_of_no_air_media_is_refused_too(self):
        parent = media(airing_prohibited=True)
        edit = media(kind=ProductionMedia.KIND_EDIT, derived_from=parent, airing_prohibited=True)
        with self.assertRaises(transitions.EvidenceRefused):
            transitions.submit(self.item(), edit, actor=self.owner)

    def test_ordinary_media_is_unchanged(self):
        self.assertEqual(self.validated().state, S.VALIDATED)


class NoAirRenditionTests(Fixtures):
    def test_delivery_never_binds_a_no_air_rendition(self):
        """Delivery's own binding is the same fail-closed authority: even a
        rendition row that somehow carried the restriction is refused before
        any provenance or projection work."""
        source = media(airing_prohibited=True)
        rendition = media(kind=ProductionMedia.KIND_RENDITION, derived_from=source, airing_prohibited=True)
        row = SimpleNamespace(rendition_media_id=rendition.pk, submission_media_id=source.pk, content_type=None)
        with self.assertRaises(delivery.ProjectionError) as raised, transaction.atomic():
            delivery._bind_rendition(row)
        self.assertEqual(raised.exception.code, "airing_prohibited")
