"""Codex Blocker 1: ingest_derivative() vs purge_media(), real sessions.

Every test drives the PUBLIC ingest_derivative() API in one thread (its own
PostgreSQL session) and the real purge_media() in another. Interleavings are
forced with events at two seams that do not change behaviour:

* after the derivative's final transaction has taken the parent's binding
  lock (a wrapper around retention.lock_for_binding);
* after the purge has taken the same row lock (a wrapper around
  retention.find_references, which purge_media calls under the lock).

"Blocked" is proven from pg_stat_activity (a row-lock wait), never assumed.
"""
import io
import threading
import time

from django.db import connection, connections
from django.test import TransactionTestCase

from production.errors import IntakeError, PurgeRefused
from production.models import ProductionMedia
from production.services import intake, layout, retention

from .support import IsolatedMediaRootMixin, fixture

WAIT = 30
REAL_LOCK_FOR_BINDING = retention.lock_for_binding
REAL_FIND_REFERENCES = retention.find_references


class _Abort(Exception):
    """Makes the paused transaction roll back."""


class BindingRaceTests(IsolatedMediaRootMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.parent = intake.ingest_stream(io.BytesIO(fixture("wav16_mono.wav")), kind="recording",
                                           validate=True).media
        self.assertTrue(self.parent.is_valid)

    # -- harness -----------------------------------------------------------
    def run_thread(self, name, target):
        box = {"error": None, "result": None}

        def runner():
            try:
                box["result"] = target()
            except BaseException as exc:                    # noqa: BLE001 -- reported to the test
                box["error"] = exc
            finally:
                connections.close_all()
        thread = threading.Thread(target=runner, name=name, daemon=True)
        thread.start()
        return thread, box

    def wait_until_blocked(self):
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock' AND wait_event = 'transactionid'",
                )
                if cursor.fetchone()[0]:
                    return
            time.sleep(0.02)
        self.fail("the second session never blocked on the ProductionMedia row lock")

    def pause_binder_after_lock(self, *, abort=False):
        locked, release = threading.Event(), threading.Event()

        def wrapper(media, **kwargs):
            row = REAL_LOCK_FOR_BINDING(media, **kwargs)
            if threading.current_thread().name == "binder":
                locked.set()
                assert release.wait(WAIT)
                if abort:
                    raise _Abort("binder rolls back after taking the lock")
            return row
        return wrapper, locked, release

    def pause_purger_after_lock(self, *, abort=False):
        locked, release = threading.Event(), threading.Event()

        def wrapper(media):
            if threading.current_thread().name == "purger":
                locked.set()
                assert release.wait(WAIT)
                if abort:
                    raise _Abort("purge rolls back after taking the lock")
            return REAL_FIND_REFERENCES(media)
        return wrapper, locked, release

    def derive(self, parent=None):
        return intake.ingest_derivative(parent or self.parent, io.BytesIO(fixture("flac.flac")),
                                        kind="edit", validate=True)

    def children(self):
        return list(ProductionMedia.objects.filter(derived_from=self.parent.pk))

    def parent_state(self):
        return ProductionMedia.objects.get(pk=self.parent.pk).retention_state

    def parent_bytes_exist(self):
        return layout.resolve_storage_path(self.parent.storage_key).exists()

    # -- the four orderings ---------------------------------------------------
    def test_binder_wins_purge_waits_then_refuses_because_the_child_exists(self):
        from unittest import mock
        wrapper, locked, release = self.pause_binder_after_lock()
        with mock.patch.object(retention, "lock_for_binding", side_effect=wrapper):
            binder, binder_box = self.run_thread("binder", self.derive)
            self.assertTrue(locked.wait(WAIT))
            purger, purger_box = self.run_thread("purger", lambda: retention.purge_media(self.parent))
            self.wait_until_blocked()                                   # the purge is genuinely waiting
            release.set()
            binder.join(WAIT)
            purger.join(WAIT)
        self.assertIsNone(binder_box["error"])
        child = binder_box["result"].media
        self.assertEqual(child.derived_from_id, self.parent.pk)
        self.assertIsInstance(purger_box["error"], PurgeRefused)
        self.assertEqual(purger_box["error"].references, (("production.ProductionMedia", "derived_from", 1),))
        self.assertEqual(self.parent_state(), "present")
        self.assertTrue(self.parent_bytes_exist())

    def test_purge_wins_derivative_waits_then_refuses_because_the_parent_is_purged(self):
        from unittest import mock
        wrapper, locked, release = self.pause_purger_after_lock()
        with mock.patch.object(retention, "find_references", side_effect=wrapper):
            purger, purger_box = self.run_thread("purger", lambda: retention.purge_media(self.parent))
            self.assertTrue(locked.wait(WAIT))
            binder, binder_box = self.run_thread("binder", self.derive)
            self.wait_until_blocked()                                   # streamed + validated, now waiting
            release.set()
            purger.join(WAIT)
            binder.join(WAIT)
        self.assertIsNone(purger_box["error"])
        self.assertEqual(self.parent_state(), "purged")
        self.assertIsInstance(binder_box["error"], IntakeError)
        self.assertEqual(binder_box["error"].code, "parent_purged")
        self.assertEqual(self.children(), [])                           # nothing references purged media
        self.assertEqual(self.list_files("media") + self.list_files("incoming"), [])   # child bytes cleaned up

    def test_binder_rollback_lets_the_purge_succeed(self):
        from unittest import mock
        wrapper, locked, release = self.pause_binder_after_lock(abort=True)
        with mock.patch.object(retention, "lock_for_binding", side_effect=wrapper):
            binder, binder_box = self.run_thread("binder", self.derive)
            self.assertTrue(locked.wait(WAIT))
            purger, purger_box = self.run_thread("purger", lambda: retention.purge_media(self.parent))
            self.wait_until_blocked()
            release.set()
            binder.join(WAIT)
            purger.join(WAIT)
        self.assertIsInstance(binder_box["error"], _Abort)
        self.assertIsNone(purger_box["error"])
        self.assertEqual(self.parent_state(), "purged")
        self.assertEqual(self.children(), [])
        self.assertEqual(self.list_files("media") + self.list_files("incoming"), [])

    def test_purge_rollback_lets_the_derivative_succeed(self):
        from unittest import mock
        wrapper, locked, release = self.pause_purger_after_lock(abort=True)
        with mock.patch.object(retention, "find_references", side_effect=wrapper):
            purger, purger_box = self.run_thread("purger", lambda: retention.purge_media(self.parent))
            self.assertTrue(locked.wait(WAIT))
            binder, binder_box = self.run_thread("binder", self.derive)
            self.wait_until_blocked()
            release.set()
            purger.join(WAIT)
            binder.join(WAIT)
        self.assertIsInstance(purger_box["error"], _Abort)
        self.assertIsNone(binder_box["error"])
        self.assertEqual(self.parent_state(), "present")
        self.assertEqual([child.pk for child in self.children()], [binder_box["result"].media.pk])
        self.assertTrue(self.parent_bytes_exist())

    # -- the exact Codex interleaving, deterministically --------------------------------
    def test_a_purge_committed_while_the_child_streams_is_caught_by_the_final_lock(self):
        """The early parent check passes, then the parent is purged and
        committed (its own session) while the child is still streaming; the
        final binding lock must re-read the truth and refuse."""
        purged = {}
        parent = self.parent

        class PurgingSource(io.BytesIO):
            def read(inner, size=-1):
                if not purged:
                    worker, box = self.run_thread("purger", lambda: retention.purge_media(parent))
                    worker.join(WAIT)
                    purged["box"] = box
                return super().read(size)

        with self.assertRaises(IntakeError) as caught:
            intake.ingest_derivative(parent, PurgingSource(fixture("flac.flac")), kind="edit", validate=True)
        self.assertIsNone(purged["box"]["error"])
        self.assertEqual(caught.exception.code, "parent_purged")
        self.assertEqual(self.parent_state(), "purged")
        self.assertEqual(self.children(), [])
        self.assertEqual(self.list_files("media") + self.list_files("incoming"), [])

    def test_a_stale_in_memory_parent_cannot_bypass_the_locked_revalidation(self):
        stale = ProductionMedia.objects.get(pk=self.parent.pk)
        worker, box = self.run_thread("purger", lambda: retention.purge_media(self.parent))
        worker.join(WAIT)
        self.assertIsNone(box["error"])
        self.assertTrue(stale.is_present and stale.is_valid)            # the object still claims it
        with self.assertRaises(IntakeError) as caught:
            intake.ingest_derivative(stale, io.BytesIO(fixture("flac.flac")), kind="edit", validate=True)
        self.assertIn(caught.exception.code, ("parent_not_usable", "parent_purged"))
        # And past the advisory early check, the lock itself still refuses:
        from unittest import mock
        with mock.patch.object(intake, "_check_request", return_value=stale), \
                self.assertRaises(IntakeError) as caught:
            intake.ingest_derivative(stale, io.BytesIO(fixture("flac.flac")), kind="edit", validate=True)
        self.assertEqual(caught.exception.code, "parent_purged")
        self.assertEqual(self.children(), [])

    def test_the_derivative_takes_the_binding_lock_inside_its_row_transaction(self):
        from unittest import mock
        seen = {}

        def spy(media, **kwargs):
            seen["in_atomic"] = connection.in_atomic_block
            seen["require_valid"] = kwargs.get("require_valid")
            return REAL_LOCK_FOR_BINDING(media, **kwargs)
        with mock.patch.object(retention, "lock_for_binding", side_effect=spy):
            child = self.derive().media
        self.assertEqual(seen, {"in_atomic": True, "require_valid": True})
        self.assertEqual(child.derived_from_id, self.parent.pk)
