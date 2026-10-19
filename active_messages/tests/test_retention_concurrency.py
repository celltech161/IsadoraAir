"""D3E-C concurrency: cleanup racing the operations it must never disturb.

Real threads, real transactions (TransactionTestCase, seeded catalog
restored), real publications (d3ec_support). Each race either serializes on
the established locks (policy -> execution -> Track; the media row lock;
the publication destination flock) or refuses -- never both outcomes, never
a deadlock, never lost history.
"""
import threading
import time
import unittest
from datetime import timedelta

from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from active_messages import playout, retention, services, worker
from active_messages.models import ActiveBroadcastMessageEvent, ActiveBroadcastPublication
from library.models import Track
from production.errors import PurgeRefused
from production.models import ProductionMedia
from production.services import retention as production_retention
from production.tests.support import ffmpeg_available

from .d3ec_support import Lifecycle


def run_threads(*targets, timeout=60):
    results, errors = {}, {}

    def wrap(name, target):
        try:
            results[name] = target()
        except BaseException as exc:                 # noqa: BLE001 -- reported to the test
            errors[name] = exc
        finally:
            connection.close()

    threads = [threading.Thread(target=wrap, args=(name, target)) for name, target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
        assert not thread.is_alive(), "deadlock: a thread did not finish"
    return results, errors


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class CleanupRaceTests(Lifecycle, TransactionTestCase):
    serialized_rollback = True

    def retire_now(self, execution):
        return lambda: retention.retire_publication(execution.pk, apply=True)

    def test_two_concurrent_retirements_retire_once(self):
        execution, publication, _item = self.completed()
        results, errors = run_threads(("a", self.retire_now(execution)), ("b", self.retire_now(execution)))
        self.assertEqual(errors, {})
        self.assertEqual(sorted(outcome.action for outcome in results.values()), ["already_retired", "retired"])
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(event=retention.RETIRED_EVENT).count(), 1)
        self.assertFalse(self.file(publication).exists())

    def test_retirement_racing_a_library_track_deletion(self):
        from library.views import _delete_track_and_file
        execution, publication, _item = self.completed()

        def library_delete():
            track = Track.objects.filter(pk=publication.track_id_snapshot).first()
            return _delete_track_and_file(track) if track is not None else (True, None)

        results, errors = run_threads(("retire", self.retire_now(execution)), ("library", library_delete))
        self.assertEqual(errors, {})
        self.assertIn(results["retire"].action, ("retired", "refused"))
        self.assertFalse(Track.objects.filter(pk=publication.track_id_snapshot).exists())
        self.assertFalse(self.file(publication).exists())
        stored = ActiveBroadcastPublication.objects.get(pk=publication.pk)
        self.assertEqual((stored.track_id, stored.track_id_snapshot), (None, publication.track_id_snapshot))
        if results["retire"].action == "refused":                # the library won: now retire the remains
            self.assertEqual(self.retire(execution).action, "retired")
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(event=retention.RETIRED_EVENT).count(), 1)

    def test_retirement_racing_a_stale_engine_admission_and_a_worker_pass(self):
        execution, publication, _item = self.completed()
        stale_command = playout.command_payload(execution, publication, execution.attempt_token)

        def admit():
            with transaction.atomic():
                try:
                    playout.admit(stale_command)
                except playout.Refused as exc:
                    return str(exc)
            return "admitted"

        results, errors = run_threads(("retire", self.retire_now(execution)), ("admit", admit),
                                      ("worker", lambda: worker.run_once().result))
        self.assertEqual(errors, {})
        self.assertEqual(results["retire"].action, "retired")
        self.assertNotEqual(results["admit"], "admitted")         # a terminal execution never re-enters
        self.assertEqual(self.stored(execution).state, "completed")


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class PurgeBindingRaceTests(Lifecycle, TransactionTestCase):
    """An unbound aggregate purge racing the worker's bind of that very
    rendition: the media row lock serializes them; exactly one wins."""

    serialized_rollback = True

    def pending_with_rendition(self):
        from active_messages import rendering
        self.real_active(self.producer, kind="urgent", seconds=1.0)
        execution = services.materialize_due_execution()
        media = rendering.render(execution).media
        return execution, media

    def hold(self, operation, entered, release):
        def run():
            with transaction.atomic():
                result = operation()
                entered.set()
                release.wait(10)
            return result
        return run

    def test_purge_first_then_bind_is_refused(self):
        execution, media = self.pending_with_rendition()
        entered, release = threading.Event(), threading.Event()

        def bind():
            entered.wait(10)
            threading.Timer(0.5, release.set).start()           # the bind now waits on the row lock
            try:
                services.bind_execution_mix(execution, media)
            except services.MixRefused as exc:
                return exc.code
            return "bound"

        results, errors = run_threads(("purge", self.hold(lambda: production_retention.purge_media(media),
                                                          entered, release)), ("bind", bind))
        self.assertEqual(errors, {})
        self.assertEqual(results["bind"], "rendition_unavailable")   # the worker simply renders again
        self.assertEqual(ProductionMedia.objects.get(pk=media.pk).retention_state, "purged")
        self.assertEqual(self.stored(execution).state, "pending_render")

    def test_bind_first_then_purge_is_refused(self):
        execution, media = self.pending_with_rendition()
        entered, release = threading.Event(), threading.Event()

        def explicit_purge():
            entered.wait(10)
            threading.Timer(0.5, release.set).start()
            try:
                production_retention.purge_media(media)
            except PurgeRefused:
                return "refused"
            return "purged"

        results, errors = run_threads(("bind", self.hold(lambda: services.bind_execution_mix(execution, media),
                                                         entered, release)), ("purge", explicit_purge))
        self.assertEqual(errors, {})
        self.assertEqual(results["purge"], "refused")
        self.assertEqual(self.stored(execution).state, "ready")
        self.assertTrue(ProductionMedia.objects.get(pk=media.pk).is_present)
        later = timezone.now() + timedelta(hours=25)
        self.assertEqual(retention.purge_unbound_aggregates(apply=True, now=later).outcomes, [])   # bound now


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class SweepPublicationRaceTests(Lifecycle, TransactionTestCase):
    serialized_rollback = True

    def test_a_publish_adopting_its_placed_file_races_the_sweep(self):
        execution = self.ready_execution()
        placed = self.library_root / playout.relpath_for(execution.pk)
        placed.parent.mkdir(parents=True, exist_ok=True)
        from production.services import media_io
        opened = media_io.open_media(execution.mix_media)
        try:
            placed.write_bytes(opened.file.read())
        finally:
            opened.close()
        later = timezone.now() + timedelta(hours=25)

        def sweep():
            time.sleep(0.05)
            return [(o.action, o.reasons) for o in
                    retention.sweep_publication_namespace(apply=True, now=later).outcomes]

        results, errors = run_threads(("publish", lambda: playout.publish(execution)), ("sweep", sweep))
        self.assertEqual(errors, {})
        self.assertTrue(placed.is_file())                        # adopted, never swept
        publication = ActiveBroadcastPublication.objects.get(execution=execution)
        self.assertEqual(playout._file_sha256(placed), publication.sha256)
        self.assertNotIn("removed", [action for action, _r in results["sweep"]])
