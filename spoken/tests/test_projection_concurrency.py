"""C2B -- concurrency with REAL PostgreSQL sessions (one connection per
thread): every race between projection operations ends in one coherent
state, with no deadlock -- the global lock order (type -> item -> asset ->
editions -> media -> sorted reference keys -> Track) is the same everywhere.
Plus the multi-reference lock-order regression, and the stale-promotion
compare-and-write."""
import threading
import time
import unittest
from datetime import timedelta

from django.db import OperationalError, connections, transaction
from django.test import TransactionTestCase

from library.models import Track
from production.services import reference_audio
from spoken import delivery, guard, transitions
from spoken.models import AssetState, DeliveryMode, EditionState, ItemState, SpokenAsset, SpokenAssetEdition, SpokenItem

from .projection_support import FFMPEG, ProjectionFixtures, ProjectionMixin
from .test_domain import make_category

S = ItemState
ALLOWED = ("stale_promotion", "delivery_in_progress", "projection_in_transition", "asset_detached",
           "not_delivered", "not_deliverable", "not_due")


def race(*operations, timeout=120):
    """Run each operation in its own thread (own DB session), released
    together. Returns [(result, exception)] in order."""
    barrier = threading.Barrier(len(operations))
    outcomes = [None] * len(operations)

    def run(index, operation):
        try:
            barrier.wait(30)
            outcomes[index] = (operation(), None)
        except BaseException as exc:                     # noqa: BLE001 -- reported to the test
            outcomes[index] = (None, exc)
        finally:
            connections.close_all()
    threads = [threading.Thread(target=run, args=pair) for pair in enumerate(operations)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
        if thread.is_alive():
            raise AssertionError("a racing operation never finished (deadlock?)")
    return outcomes


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class ProjectionRaceTests(ProjectionMixin, TransactionTestCase):
    def assert_no_unexpected_failure(self, outcomes):
        for _result, exc in outcomes:
            if exc is None:
                continue
            self.assertNotIsInstance(exc, OperationalError, f"database error (deadlock?): {exc}")
            if isinstance(exc, guard.SpokenIntegrityError):
                self.assertIn(
                    str(exc),
                    {
                        "the projected Track cannot be deleted while replacement is in progress",
                        "reconcile the projection barrier before deleting its Track",
                    },
                    repr(exc),
                )
                continue
            self.assertIn(getattr(exc, "code", None), ALLOWED, repr(exc))

    def assert_coherent(self, ctype):
        """At most one non-retired current-edition asset and one Track for
        it; at most one active edition; the file is exactly the active
        edition (or absent); the Track is ready only with proven bytes."""
        assets = list(SpokenAsset.objects.filter(content_type=ctype).exclude(state=AssetState.RETIRED))
        if ctype.delivery_mode == DeliveryMode.CURRENT_EDITION:
            self.assertLessEqual(len(assets), 1)
        for asset in assets:
            active = list(SpokenAssetEdition.objects.filter(asset=asset, state=EditionState.ACTIVE))
            self.assertLessEqual(len(active), 1)
            on_disk = self.sha_on_disk(asset.library_relpath)
            if asset.state == AssetState.LIVE:
                self.assertEqual(len(active), 1)
                self.assertEqual(asset.current_item_id, active[0].item_id)
                self.assertEqual(on_disk, active[0].library_sha256)
                self.assertEqual(SpokenItem.objects.get(pk=asset.current_item_id).state, S.DELIVERED)
            else:
                self.assertEqual(active, [])
                self.assertIsNone(on_disk)
            if asset.track_id is not None:
                track = Track.objects.get(pk=asset.track_id)
                self.assertEqual(track.ready2air, asset.state == AssetState.LIVE)
                self.assertEqual(track.filepath, str(self.library_file(asset.library_relpath)))
        self.assertEqual(Track.objects.filter(spoken_asset__content_type=ctype).count(),
                         len([a for a in assets if a.track_id]))

    def test_two_first_deliveries_make_one_asset_and_one_track(self):
        ctype = self.make_type()
        a, b = self.processed(ctype), self.processed(ctype)
        outcomes = race(lambda: delivery.deliver_item(a), lambda: delivery.deliver_item(b))
        self.assert_no_unexpected_failure(outcomes)
        self.assert_coherent(ctype)
        self.assertEqual(SpokenAsset.objects.filter(content_type=ctype).count(), 1)
        self.assertEqual(Track.objects.filter(spoken_asset__isnull=False).count(), 1)

    def test_the_same_item_delivered_twice_delivers_once(self):
        ctype = self.make_type(DeliveryMode.ACCUMULATING, category="PSA")
        item = self.processed(ctype)
        outcomes = race(lambda: delivery.deliver_item(item), lambda: delivery.deliver_item(item))
        self.assert_no_unexpected_failure(outcomes)
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.DELIVERED)
        self.assertEqual((SpokenAsset.objects.count(), SpokenAssetEdition.objects.filter(state="active").count(),
                          Track.objects.filter(spoken_asset__isnull=False).count()), (1, 1, 1))
        self.assertEqual(list(SpokenItem.objects.get(pk=item.pk).events.values_list("event", flat=True)).count(
            "delivered"), 1)
        self.assert_coherent(ctype)

    def test_delivery_against_recall_expiry_and_detach(self):
        for contender in ("recall", "expire", "detach"):
            with self.subTest(contender=contender):
                ctype = self.make_type(default_lifetime=timedelta(seconds=1))
                old, first = self.delivered(ctype)
                newer = self.processed(ctype)
                time.sleep(1.1)                                   # the old item is due
                if contender == "recall":
                    other = lambda: delivery.recall_delivered(old, actor=self.manager)       # noqa: E731
                elif contender == "expire":
                    other = lambda: delivery.expire_item(old)                                # noqa: E731
                else:
                    other = lambda: Track.objects.get(pk=first.track_id).delete()            # noqa: E731
                outcomes = race(lambda: delivery.deliver_item(newer), other)
                self.assert_no_unexpected_failure(outcomes)
                delivery.reconcile()                              # (post-commit file removals are done)
                self.assert_coherent(ctype)
                asset = SpokenAsset.objects.get(content_type=ctype)
                if contender == "detach":
                    self.assertIn(asset.state, (AssetState.DETACHED,))
                    self.assertIsNone(asset.track_id)             # a deleted Track is never recreated
                else:
                    self.assertEqual((asset.state, asset.current_item_id), (AssetState.LIVE, newer.pk))
                    self.assertIn(SpokenItem.objects.get(pk=old.pk).state, (S.RECALLED, S.EXPIRED))

    def test_operator_detach_against_recall_and_expiry(self):
        for contender in ("recall", "expire"):
            with self.subTest(contender=contender):
                ctype = self.make_type(default_lifetime=timedelta(seconds=1))
                item, result = self.delivered(ctype)
                time.sleep(1.1)
                if contender == "recall":
                    other = lambda: delivery.recall_delivered(item, actor=self.manager)      # noqa: E731
                else:
                    other = lambda: delivery.expire_item(item)                               # noqa: E731
                outcomes = race(lambda: Track.objects.get(pk=result.track_id).delete(), other)
                self.assert_no_unexpected_failure(outcomes)
                delivery.reconcile()
                asset = SpokenAsset.objects.get(content_type=ctype)
                self.assertFalse(Track.objects.filter(pk=result.track_id).exists())     # never recreated
                self.assertIn(asset.state, (AssetState.DETACHED, AssetState.PENDING))
                if asset.state == AssetState.DETACHED:
                    self.assertIsNone(asset.track_id)
                self.assertIn(SpokenItem.objects.get(pk=item.pk).state, (S.RECALLED, S.EXPIRED))
                self.assertIsNone(self.sha_on_disk(asset.library_relpath))

    def test_reconcile_and_relocation_against_delivery(self):
        ctype = self.make_type()
        self.delivered(ctype)
        newer = self.processed(ctype)
        outcomes = race(lambda: delivery.deliver_item(newer), lambda: delivery.reconcile())
        self.assert_no_unexpected_failure(outcomes)
        self.assert_coherent(ctype)
        ctype.target_category = make_category("NewsDesk")
        ctype.save()
        latest = self.processed(ctype)
        outcomes = race(lambda: delivery.deliver_item(latest), lambda: delivery.reconcile())
        self.assert_no_unexpected_failure(outcomes)
        delivery.reconcile()
        asset = SpokenAsset.objects.get(content_type=ctype)
        self.assertTrue(asset.library_relpath.startswith("NewsDesk/"))
        self.assertEqual(sorted(p.parent.name for p in self.library.rglob("spoken-*.flac")), ["NewsDesk"])
        self.assert_coherent(ctype)

    def test_barrier_gap_waits_recall_expiry_and_refuses_detach_reconcile(self):
        """The session lock covers the gap after barrier commit and before
        TX-C takes row locks; contenders fail/skip instead of interleaving."""
        from unittest import mock
        from spoken import guard

        for contender in ("recall", "expire", "detach", "reconcile"):
            with self.subTest(contender=contender):
                ctype = self.make_type(code=f"barrier-{contender}", default_lifetime=timedelta(seconds=1))
                old, first = self.delivered(ctype)
                newer = self.processed(ctype)
                if contender == "expire":
                    time.sleep(1.1)
                barrier, release = threading.Event(), threading.Event()
                outcome = []

                def pause(point):
                    if point == "barrier_committed":
                        barrier.set()
                        if not release.wait(20):
                            raise AssertionError("test did not release the barrier")

                def promote():
                    try:
                        outcome.append((delivery.deliver_item(newer), None))
                    except BaseException as exc:  # noqa: BLE001 -- asserted below
                        outcome.append((None, exc))
                    finally:
                        connections.close_all()

                with mock.patch.object(delivery, "_fault", pause):
                    thread = threading.Thread(target=promote)
                    thread.start()
                    self.assertTrue(barrier.wait(20), "delivery did not commit its barrier")
                    end_outcome, end_thread = [], None
                    if contender in ("recall", "expire", "detach"):
                        def end_item():
                            try:
                                if contender == "recall":
                                    result = delivery.recall_delivered(old, actor=self.manager)
                                elif contender == "expire":
                                    result = delivery.expire_item(old)
                                else:
                                    result = Track.objects.get(pk=first.track_id).delete()
                                end_outcome.append((result, None))
                            except BaseException as exc:  # noqa: BLE001 -- asserted below
                                end_outcome.append((None, exc))
                            finally:
                                connections.close_all()
                        end_thread = threading.Thread(target=end_item)
                        end_thread.start()
                        time.sleep(0.2)
                        self.assertTrue(end_thread.is_alive(), f"{contender} did not wait for the barrier")
                    else:
                        report = delivery.reconcile()
                        self.assertIn("busy", [finding["finding"] for finding in report])
                    release.set()
                    thread.join(30)
                    self.assertFalse(thread.is_alive(), "delivery deadlocked")
                    if end_thread is not None:
                        end_thread.join(30)
                        self.assertFalse(end_thread.is_alive(), "recall/expiry deadlocked")
                        self.assertEqual(len(end_outcome), 1)
                        self.assertIsNone(end_outcome[0][1], repr(end_outcome[0][1]))
                self.assertEqual(len(outcome), 1)
                self.assertIsNone(outcome[0][1], repr(outcome[0][1]))
                asset = SpokenAsset.objects.get(content_type=ctype)
                if contender == "detach":
                    self.assertEqual(asset.state, AssetState.DETACHED)
                    self.assertIsNone(asset.track_id)
                    self.assertFalse(Track.objects.filter(pk=first.track_id).exists())
                else:
                    self.assertEqual((asset.state, asset.current_item_id), (AssetState.LIVE, newer.pk))
                if contender in ("recall", "expire"):
                    self.assertEqual(SpokenItem.objects.get(pk=old.pk).state,
                                     S.RECALLED if contender == "recall" else S.EXPIRED)


class ReferenceKeyLockOrderTests(TransactionTestCase):
    """The future multi-reference gate: two transactions locking the SAME
    keys in opposite request orders. lock_keys takes them in one global order,
    so neither can hold one while waiting for the other."""

    def test_opposite_request_orders_never_deadlock(self):
        keys = ["bed.alpha", "bed.omega"]
        first_taken = threading.Barrier(2)
        real = reference_audio.lock_key

        def slow_lock(key, *, shared=False):
            real(key, shared=shared)
            try:
                first_taken.wait(3)          # both try to hold one key before taking the next
            except threading.BrokenBarrierError:
                pass

        def take(order):
            with transaction.atomic():
                reference_audio.lock_keys(order)
                time.sleep(0.2)
            return order
        from unittest import mock
        with mock.patch.object(reference_audio, "lock_key", slow_lock):
            outcomes = race(lambda: take(keys), lambda: take(list(reversed(keys))), timeout=60)
        for _result, exc in outcomes:
            self.assertIsNone(exc, f"deadlock or failure: {exc!r}")


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class StalePromotionTests(ProjectionFixtures):
    def test_a_stale_promotion_never_overwrites_a_newer_edition(self):
        ctype = self.make_type()
        self.delivered(ctype)
        a, b = self.processed(ctype), self.processed(ctype)
        newer, started = [], []

        def deliver_b_meanwhile(point):
            if point == "pending_created" and not started:
                started.append(True)
                newer.append(delivery.deliver_item(b))           # B is delivered completely in between
        from unittest import mock
        with mock.patch.object(delivery, "_fault", deliver_b_meanwhile):
            with self.assertRaises(delivery.ProjectionError) as raised:
                delivery.deliver_item(a)
        self.assertEqual(raised.exception.code, "stale_promotion")
        asset = SpokenAsset.objects.get(content_type=ctype)
        self.assertEqual((asset.current_item_id, asset.generation), (b.pk, newer[0].generation))
        self.assertEqual(self.sha_on_disk(asset.library_relpath), b.rendition_media.sha256)
        self.assertEqual(SpokenAssetEdition.objects.get(asset=asset, item=a).state, EditionState.ABORTED)
        self.assertEqual(SpokenItem.objects.get(pk=a.pk).state, S.PROCESSED)
