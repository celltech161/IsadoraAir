"""r0113 / D3C correction: TEST / NO AIR media can never reach air.

The confirmed blocker: a take saved in the TEST workspace could be committed
to a REGULAR draft, rendered, and delivered as an ordinary Ready-to-Air Track.
The fix is media provenance, not a binding lookup: every take the TEST
recorder makes (record, import, edit) is NO-AIR ProductionMedia from intake on
-- immutable and inherited by every derivative -- and the canonical
air-capable binding refuses it. So these must hold whether or not the media is
(still) a test take:

* the TEST recorder stamps every take; the regular recorder never does, but
  an edit of NO-AIR media made in the regular workspace stays NO-AIR;
* no request parameter can choose or clear it;
* the regular path refuses it over HTTP and through regular.submit (a lower
  level spoken API refusal is proven in spoken.tests.test_no_air_media, and
  VoiceTrack / active messages / reference audio in their own apps);
* a never-saved TEST take, and an ex-take kept alive by a derivative after
  its binding was discarded, are refused the same way;
* ordinary media never becomes a test take;
* in real concurrent PostgreSQL transactions, either order of a TEST save and
  a regular submission ends with nothing airable.
"""
import io
import threading
import time

from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from library.models import Track
from produced_content import no_air, regular
from produced_content.models import ProducedContentTestTake
from production.models import ProductionMedia
from production.services import intake
from production.tests.support import fixture
from spoken import worker
from spoken.models import ItemState, SpokenAsset, SpokenItem

from .support import API, PAGE, People, assign, media_row, no_air_type, regular_type

S = ItemState
MEDIA_TABLE = ProductionMedia._meta.db_table


def nothing_airable_from(test, media_ids):
    """No submission, rendition, asset or Track exists for these media."""
    test.assertFalse(SpokenItem.objects.filter(submission_media_id__in=media_ids).exists())
    test.assertFalse(ProductionMedia.objects.filter(kind=ProductionMedia.KIND_RENDITION).exists())
    test.assertFalse(SpokenAsset.objects.exists())
    test.assertFalse(Track.objects.exists())


class Workspace(People):
    def setUp(self):
        super().setUp()
        self.practice = no_air_type("practice", name="Practice")
        self.news = regular_type("news", output_mode="accumulating", recipe_key="lcl_psa", category_code="PCPSA")
        assign(self.producer, self.practice, self.news)

    TEST = "test_type=practice"

    def media(self, media_id):
        return ProductionMedia.objects.get(pk=media_id)

    def context_revision(self, query):
        response = self.context(self.producer, query)
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["context"]["revision"]

    def save_test(self, media_id):
        with self.captureOnCommitCallbacks(execute=True):
            return self.commit(self.producer, {"test_type": "practice"}, media_id, self.context_revision(self.TEST))

    def draft(self):
        response = self.post(self.producer, f"{PAGE}start/", {"content_type": self.news.key, "title": "t"})
        self.assertEqual(response.status_code, 302)
        return SpokenItem.objects.filter(submitted_by=self.producer).order_by("-created_at").first()

    def commit_regular(self, item, media_id):
        query = f"item={item.pk}"
        return self.commit(self.producer, {"item": str(item.pk)}, media_id, self.context_revision(query))

    def edit(self, query, parent_id, extra=""):
        return self.recorded(self.producer, query, mode="edit",
                             extra=f"&derived_from={parent_id}&operations=%5B%22trim%22%5D{extra}")

    def assert_refused(self, response, code):
        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(response.json()["error"], code)


@override_settings(SECURE_SSL_REDIRECT=False)
class RecorderProvenanceTests(Workspace, TestCase):
    def test_every_test_recorder_take_is_no_air_from_intake(self):
        recorded = self.recorded(self.producer, self.TEST)
        imported = self.recorded(self.producer, self.TEST, mode="import")
        edited = self.edit(self.TEST, recorded)
        for media_id in (recorded, imported, edited):
            self.assertTrue(self.media(media_id).airing_prohibited)
        self.assertFalse(ProducedContentTestTake.objects.exists())          # stamped before any save

    def test_regular_recorder_takes_are_ordinary_but_an_edit_of_test_media_stays_no_air(self):
        test_take = self.recorded(self.producer, self.TEST)
        query = f"item={self.draft().pk}"
        ordinary = self.recorded(self.producer, query)
        self.assertFalse(self.media(ordinary).airing_prohibited)
        self.assertFalse(self.media(self.edit(query, ordinary)).airing_prohibited)
        laundered = self.edit(query, test_take)                             # edited in the REGULAR workspace
        self.assertTrue(self.media(laundered).airing_prohibited)

    def test_no_request_parameter_chooses_or_clears_the_restriction(self):
        query = f"item={self.draft().pk}"
        for forged in ("&airing_prohibited=0", "&airing_prohibited=false", "&no_air=0"):
            with self.subTest(forged=forged):
                self.assertTrue(self.media(self.recorded(self.producer, self.TEST + forged)).airing_prohibited)
                self.assertFalse(self.media(self.recorded(self.producer, query + forged.replace("0", "1")
                                                          .replace("false", "true"))).airing_prohibited)


@override_settings(SECURE_SSL_REDIRECT=False)
class RegularRefusalTests(Workspace, TestCase):
    def test_a_saved_test_take_is_refused_over_http_and_nothing_can_air(self):
        take_id = self.recorded(self.producer, self.TEST)
        self.assertEqual(self.save_test(take_id).status_code, 200)
        take = ProducedContentTestTake.objects.get()
        item = self.draft()
        self.assert_refused(self.commit_regular(item, take_id), "airing_prohibited")
        item.refresh_from_db()
        self.assertEqual((item.state, item.submission_media_id), (S.DRAFT, None))
        self.assertEqual(ProducedContentTestTake.objects.get().revision, take.revision)     # the take is untouched
        self.assertIsNone(worker.process_one(sleep=lambda s: None))                         # nothing to render
        nothing_airable_from(self, [take_id])

    def test_regular_submit_refuses_it_directly(self):
        take_id = self.recorded(self.producer, self.TEST)
        self.save_test(take_id)
        item = self.draft()
        with self.assertRaises(regular.Refused) as raised:
            regular.submit(self.producer, item, take_id, expected_revision=regular.item_revision(item))
        self.assertEqual(raised.exception.code, "airing_prohibited")
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.DRAFT)

    def test_a_never_saved_test_take_and_its_edits_are_refused(self):
        never_saved = self.recorded(self.producer, self.TEST)
        imported = self.recorded(self.producer, self.TEST, mode="import")
        item = self.draft()
        for media_id in (never_saved, imported, self.edit(f"item={item.pk}", never_saved)):
            with self.subTest(media=media_id):
                self.assert_refused(self.commit_regular(item, media_id), "airing_prohibited")
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.DRAFT)
        nothing_airable_from(self, [never_saved, imported])

    def test_an_ex_take_kept_alive_by_a_derivative_after_its_binding_is_discarded_stays_refused(self):
        take_id = self.recorded(self.producer, self.TEST)
        self.save_test(take_id)
        item = self.draft()
        kept_by = self.edit(f"item={item.pk}", take_id)                     # references the take: purge refused
        with self.captureOnCommitCallbacks(execute=True):
            no_air.discard_take(self.producer, self.practice,
                                expected_revision=no_air.take_revision(ProducedContentTestTake.objects.get()))
        self.assertFalse(ProducedContentTestTake.objects.exists())
        ex_take = self.media(take_id)
        self.assertEqual((ex_take.retention_state, ex_take.airing_prohibited), ("present", True))
        for media_id in (take_id, kept_by):
            with self.subTest(media=media_id):
                self.assert_refused(self.commit_regular(item, media_id), "airing_prohibited")
        nothing_airable_from(self, [take_id, kept_by])

    def test_ordinary_media_never_becomes_a_test_take(self):
        ordinary = self.recorded(self.producer, f"item={self.draft().pk}")
        self.assert_refused(self.save_test(ordinary), "not_test_media")
        with self.assertRaises(no_air.MediaRefused) as raised:
            no_air.save_take(self.producer, self.practice, ordinary, expected_revision=no_air.NO_TAKE_REVISION)
        self.assertEqual(raised.exception.code, "not_test_media")
        self.assertFalse(ProducedContentTestTake.objects.exists())

    def test_ordinary_regular_production_and_test_editing_are_unchanged(self):
        item = self.draft()
        ordinary = self.recorded(self.producer, f"item={item.pk}")
        self.assertEqual(self.commit_regular(item, ordinary).status_code, 200)
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.VALIDATED)
        second = self.draft()                                               # ordinary reuse across drafts
        self.assertEqual(self.commit_regular(second, ordinary).status_code, 200)
        first = self.recorded(self.producer, self.TEST)                     # TEST: save, edit, replace, remove
        self.assertEqual(self.save_test(first).status_code, 200)
        self.assertEqual(self.save_test(self.edit(self.TEST, first)).status_code, 200)
        self.assertEqual(ProducedContentTestTake.objects.get().revision, 2)
        response = self.post_json(self.producer, f"{API}remove/", {"subject": {"test_type": "practice"},
                                                                   "revision": self.context_revision(self.TEST)})
        self.assertEqual(response.status_code, 200, response.content)


def _wait_for_media_lock_waiter(test, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                           "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock' AND query ILIKE %s",
                           [f"%{MEDIA_TABLE}%"])
            if cursor.fetchone()[0]:
                return
        time.sleep(0.005)
    test.fail("nobody waited for the media row lock")


class _Party(threading.Thread):
    def __init__(self, body):
        super().__init__(daemon=True)
        self.body, self.result, self.error = body, None, None

    def run(self):
        try:
            self.result = self.body()
        except BaseException as exc:          # noqa: BLE001 -- surfaced by finish()
            self.error = exc
        finally:
            connection.close()

    def finish(self):
        self.join(60)
        if self.is_alive():
            raise AssertionError("party did not finish")
        return self.result, self.error


class ConcurrentProvenanceTests(People, TransactionTestCase):
    """Real concurrent transactions; the waiter is observed in pg_stat_activity."""

    serialized_rollback = True

    def setUp(self):
        super().setUp()
        self.practice = no_air_type("practice")
        self.news = regular_type("news", output_mode="accumulating", recipe_key="lcl_psa", category_code="PCPSA")
        assign(self.producer, self.practice, self.news)

    def submit(self, item, media):
        item = SpokenItem.objects.get(pk=item.pk)
        return regular.submit(self.producer, item, media, expected_revision=regular.item_revision(item))

    def holding(self, body):
        ready, release = threading.Event(), threading.Event()

        def hold():
            with transaction.atomic():
                result = body()
                ready.set()
                release.wait(30)
            return result
        party = _Party(hold)
        party.start()
        self.assertTrue(ready.wait(30))
        return party, release

    def test_a_test_save_holding_the_media_lock_first_then_the_submission_waits_and_is_refused(self):
        no_air_media = media_row(self.producer, airing_prohibited=True)
        item = regular.start(self.producer, self.news)
        holder, release = self.holding(lambda: no_air.save_take(self.producer, self.practice, no_air_media,
                                                                expected_revision=no_air.NO_TAKE_REVISION).pk)
        submitter = _Party(lambda: self.submit(item, no_air_media))
        submitter.start()
        _wait_for_media_lock_waiter(self)                                  # the submission waits for the save
        release.set()
        take_pk, error = holder.finish()
        self.assertIsNone(error)
        _, error = submitter.finish()
        self.assertIsInstance(error, regular.Refused)
        self.assertEqual(error.code, "airing_prohibited")
        self.assertTrue(ProducedContentTestTake.objects.filter(pk=take_pk, media=no_air_media).exists())
        self.assertEqual((SpokenItem.objects.get(pk=item.pk).state), S.DRAFT)

    def test_a_submission_holding_the_media_lock_first_then_a_test_save_of_that_ordinary_media_is_refused(self):
        ordinary = media_row(self.producer)
        item = regular.start(self.producer, self.news)
        holder, release = self.holding(lambda: self.submit(item, ordinary).state)
        saver = _Party(lambda: no_air.save_take(self.producer, self.practice, ordinary,
                                                expected_revision=no_air.NO_TAKE_REVISION))
        saver.start()
        _wait_for_media_lock_waiter(self)                                  # the save waits for the submission
        release.set()
        state, error = holder.finish()
        self.assertIsNone(error)
        self.assertEqual(state, S.VALIDATED)
        _, error = saver.finish()
        self.assertIsInstance(error, no_air.MediaRefused)
        self.assertEqual(error.code, "not_test_media")
        self.assertFalse(ProducedContentTestTake.objects.exists())

    def test_no_binding_timing_opens_a_window(self):
        """A discard in flight (binding deleted, not yet committed) and after
        it commits: the submission is refused both times -- the restriction
        never depended on the binding. A derivative keeps the bytes alive (the
        discard's purge refuses), so the refusal afterwards is the
        restriction's, not a purge's."""
        no_air_media = media_row(self.producer, airing_prohibited=True)
        media_row(self.producer, kind=ProductionMedia.KIND_EDIT, derived_from=no_air_media, airing_prohibited=True)
        no_air.save_take(self.producer, self.practice, no_air_media, expected_revision=no_air.NO_TAKE_REVISION)
        first, second = regular.start(self.producer, self.news), regular.start(self.producer, self.news)
        take = ProducedContentTestTake.objects.get()
        holder, release = self.holding(lambda: no_air.discard_take(
            self.producer, self.practice, expected_revision=no_air.take_revision(take)))
        submitter = _Party(lambda: self.submit(first, no_air_media))
        submitter.start()
        _, during = submitter.finish()                                     # never waits: no binding is consulted
        release.set()
        _, error = holder.finish()
        self.assertIsNone(error)
        with self.assertRaises(regular.Refused) as after:
            self.submit(second, no_air_media)
        self.assertIsInstance(during, regular.Refused)
        self.assertEqual((during.code, after.exception.code), ("airing_prohibited", "airing_prohibited"))
        self.assertFalse(ProducedContentTestTake.objects.exists())
        self.assertEqual(ProductionMedia.objects.get(pk=no_air_media.pk).retention_state, "present")
        self.assertEqual(SpokenItem.objects.filter(state=S.DRAFT).count(), 2)


class IntakeLevelTests(People, TestCase):
    """The same media, made directly at intake, as a non-browser caller would."""

    def test_an_intake_no_air_take_is_refused_by_regular_submission(self):
        practice = no_air_type("practice")
        news = regular_type("news", output_mode="accumulating", recipe_key="lcl_psa", category_code="PCPSA")
        assign(self.producer, practice, news)
        made = intake.ingest_stream(io.BytesIO(fixture("wav16_mono.wav")), kind=ProductionMedia.KIND_RECORDING,
                                    owner=self.producer, airing_prohibited=True).media
        item = regular.start(self.producer, news)
        with self.assertRaises(regular.Refused):
            regular.submit(self.producer, item, made, expected_revision=regular.item_revision(item))
