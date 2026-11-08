"""3.11 corrections (Codex re-review M5): relocation against concurrent Library operations, in real PostgreSQL
transactions on separate connections (one thread each).

A relocation is held at a chosen point (holding its path and row locks) while another operation runs on its own
connection; the test observes that operation waiting for a lock (``pg_stat_activity``), releases the relocation,
and checks the outcome of both, the bounded waits and the final Track rows and files.  Real files in a per-test
temporary Library root; a fresh engine state in the test's own directory."""
import json
import os
import threading
import time
from pathlib import Path
from unittest import mock

from django.core import signing
from django.db import IntegrityError, connections, transaction
from django.test import Client, TransactionTestCase

from library.models import Track
from library.services import library_reconciliation as reconciliation
from library.services import library_relink
from library.services import media_relocation as m
from library.services.track_deletion import delete_track
from library.tests.test_library_category_drift import AUDIO, _DriftFixture


class _Race(_DriftFixture, TransactionTestCase):
    def thread(self, target, out, key):
        def body():
            try:
                out[key] = target()
            except BaseException as exc:
                out[key] = exc
            finally:
                connections.close_all()
        worker = threading.Thread(target=body, daemon=True)
        worker.start()
        return worker

    @staticmethod
    def lock_waiters():
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                           "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()")
            return cursor.fetchone()[0]

    def wait_for(self, predicate, seconds=10):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def put(self, rel="POP/song.flac"):
        return self.track(rel, category=self.rock, data=AUDIO)

    def dest(self, track):
        return str(self.root / "ROCK" / Path(track.filepath).name)

    def held_move(self, track, out, *, budget=5.0):
        """Start a relocation of ``track`` that pauses after its database write (holding every lock)."""
        held, release = threading.Event(), threading.Event()

        def write(locked, filepath):
            locked.filepath = filepath
            locked.save(update_fields=["filepath", "updated_at"])
            held.set()
            release.wait(20)

        worker = self.thread(lambda: m.execute(
            track_id=track.pk, source=track.filepath, destination=self.dest(track), origin="reconciliation",
            user=self.admin, write=write, lock_budget_seconds=budget,
            protection=reconciliation._protection_predicates()), out, "move")
        self.assertTrue(held.wait(10), "the relocation did not reach its hold point")
        return worker, release

    def move(self, track, budget=0.5):
        def write(locked, filepath):
            locked.filepath = filepath
            locked.save(update_fields=["filepath", "updated_at"])
        return m.execute(track_id=track.pk, source=track.filepath, destination=self.dest(track),
                         origin="reconciliation", user=self.admin, write=write, lock_budget_seconds=budget,
                         protection=reconciliation._protection_predicates())


class MoveVersusDeletionTests(_Race):
    def test_a_deletion_holding_the_track_makes_the_move_busy_and_nothing_moves(self):
        track = self.put()
        held, release, out = threading.Event(), threading.Event(), {}

        def deletion():
            with transaction.atomic():
                cleanup = delete_track(Track.objects.get(pk=track.pk), remove_source=True)
                held.set()
                release.wait(20)
            return cleanup

        worker = self.thread(deletion, out, "delete")
        self.assertTrue(held.wait(10))
        try:
            started = time.monotonic()
            result = self.move(track, budget=0.5)
            elapsed = time.monotonic() - started
        finally:
            release.set()
            worker.join(20)
        self.assertEqual(result.outcome, m.BUSY, result)
        self.assertLess(elapsed, 3.0)
        self.assertFalse(Track.objects.filter(pk=track.pk).exists())
        self.assertFalse(os.path.lexists(self.dest(track)))                 # nothing was staged
        self.assertIn(out["delete"].status, ("removed", "retained_recent"))

    def test_a_deletion_waits_for_a_held_move_and_then_deletes_the_moved_track(self):
        track = self.put()
        stale_instance = Track.objects.get(pk=track.pk)                     # loaded before the move
        out = {}
        mover, release = self.held_move(track, out)
        deleter = self.thread(lambda: delete_track(stale_instance, remove_source=True), out, "delete")
        self.assertTrue(self.wait_for(lambda: self.lock_waiters() >= 1), "the deletion did not wait")
        release.set()
        mover.join(20)
        deleter.join(20)
        self.assertIn(out["move"].outcome, (m.MOVED, m.COMMITTED_CLEANUP_REQUIRED), out["move"])
        self.assertNotIsInstance(out["delete"], Exception, out["delete"])
        self.assertFalse(Track.objects.filter(pk=track.pk).exists())
        self.assertFalse(Track.objects.filter(filepath=self.dest(track)).exists())
        # No Track names a missing file; the moved recording is never lost behind a Track that remains.
        for row in Track.objects.all():
            self.assertTrue(os.path.exists(row.filepath), row.filepath)


class MoveVersusRelinkTests(_Race):
    def test_a_relink_to_the_moves_destination_waits_and_then_fails_without_a_shared_path(self):
        moving = self.put()
        missing = self.track("POP/lost.flac", category=self.pop, create_file=False)
        plan = {"u": self.admin.pk, "track_id": missing.pk, "scan_id": "S", "finding_id": "F1", "from":
                missing.filepath, "to": self.dest(moving), "expected": {}, "identity": "x" * 64, "witness": {}}
        token = signing.dumps(plan, salt=library_relink.TOKEN_SALT)
        out = {}
        mover, release = self.held_move(moving, out)
        # The relink's evidence check (decoded audio) is not the subject here; its locks and write are real.
        with mock.patch.object(library_relink, "_check", return_value=None):
            relinker = self.thread(lambda: library_relink.apply(token, user=self.admin), out, "relink")
            self.assertTrue(self.wait_for(lambda: self.lock_waiters() >= 1), "the relink did not wait")
            release.set()
            mover.join(20)
            relinker.join(20)
        self.assertEqual(out["move"].outcome, m.MOVED)
        self.assertIsInstance(out["relink"], IntegrityError)                # the path is the moved Track's
        self.assertEqual(Track.objects.get(pk=moving.pk).filepath, self.dest(moving))
        self.assertEqual(Track.objects.get(pk=missing.pk).filepath, missing.filepath)


class MoveVersusOldSourceRegistrationTests(_Race):
    def test_a_registration_at_the_old_source_keeps_its_file(self):
        track = self.put()
        old = track.filepath
        out = {}
        mover, release = self.held_move(track, out)
        registrar = self.thread(lambda: Track.objects.create(filepath=old, filename="song.flac", title="newcomer",
                                                             artist=self.artist, category=self.pop), out, "register")
        self.assertTrue(self.wait_for(lambda: self.lock_waiters() >= 1), "the registration did not wait")
        release.set()
        mover.join(20)
        registrar.join(20)
        result = out["move"]
        self.assertEqual((result.outcome, result.cleanup), (m.COMMITTED_CLEANUP_REQUIRED, m.CLEANUP_RETAINED_OWNER))
        newcomer = out["register"]
        self.assertEqual(Track.objects.get(pk=newcomer.pk).filepath, old)
        self.assertTrue(os.path.exists(old))                                # the newcomer's file was not removed
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, self.dest(track))
        self.assertEqual(Path(self.dest(track)).read_bytes(), AUDIO)


class TrackDetailPreliminaryReadTests(_Race):
    def patch_held_between_read_and_locks(self, change):
        """Track Detail reads the row, decides the paths, then calls the service: the change commits in between, on
        another connection."""
        real = m.execute

        def execute(**kwargs):
            out = {}
            worker = self.thread(change, out, "change")
            worker.join(20)
            self.assertNotIsInstance(out.get("change"), Exception, out.get("change"))
            return real(**kwargs)
        return mock.patch.object(m, "execute", side_effect=execute)

    def patch_track(self, track, body):
        client = Client()
        client.force_login(self.admin)
        return client.patch(f"/api/tracks/{track.pk}/", data=json.dumps(body), content_type="application/json")

    def test_the_source_path_changed_before_the_locks(self):
        track = self.track("POP/detail.flac", category=self.pop, data=AUDIO)
        other = self.file("POP/elsewhere.flac", AUDIO)
        with self.patch_held_between_read_and_locks(
                lambda: Track.objects.filter(pk=track.pk).update(filepath=str(other), filename="elsewhere.flac")):
            response = self.patch_track(track, {"category_id": self.jazz.pk})
        self.assertEqual(response.status_code, 409, response.content)
        row = Track.objects.get(pk=track.pk)
        self.assertEqual((row.filepath, row.category_id), (str(other), self.pop.pk))
        self.assertFalse(os.path.lexists(self.root / "JAZZ" / "detail.flac"))
        self.assertTrue(os.path.exists(track.filepath) and other.exists())

    def test_the_primary_category_changed_before_the_locks(self):
        track = self.track("POP/recat.flac", category=self.pop, data=AUDIO)
        with self.patch_held_between_read_and_locks(
                lambda: Track.objects.filter(pk=track.pk).update(category=self.rock)):
            response = self.patch_track(track, {"category_id": self.jazz.pk})
        self.assertEqual(response.status_code, 409, response.content)
        row = Track.objects.get(pk=track.pk)
        self.assertEqual((row.filepath, row.category_id), (track.filepath, self.rock.pk))
        self.assertFalse(os.path.lexists(self.root / "JAZZ" / "recat.flac"))
