"""D3E-C concurrency: cleanup racing the operations it must never disturb.

Real threads, real transactions (TransactionTestCase, seeded catalog
restored), real publications (d3ec_support). Each race either serializes on
the established locks (policy -> execution -> Track; the media row lock;
the publication destination flock) or refuses -- never both outcomes, never
a deadlock, never lost history.
"""
import os
import threading
import time
import unittest
from datetime import timedelta

from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from active_messages import playout, reconcile, retention, services, worker
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


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class OwnershipRaceTests(Lifecycle, TransactionTestCase):
    """Review 3, across PostgreSQL sessions (one connection per thread): a
    Track registering at a retired publication's path and the cleanup that
    removes that path serialize on the destination lock; the bytes are never
    removed while any Track owns them."""

    serialized_rollback = True

    def interrupted(self):
        from unittest import mock
        execution, publication, _item = self.completed()
        with mock.patch.object(retention, "_remove_publication_bytes", side_effect=SystemExit("killed")):
            try:
                retention.retire_publication(execution.pk, apply=True)
            except SystemExit:
                pass
        self.assertTrue(retention.is_retired(execution.pk))
        return execution, publication, self.library_root / publication.library_relpath

    def new_track(self, path):
        return Track.objects.create(title="Adopted", artist=self.song_artist, filepath=str(path),
                                    filename=path.name)

    def flock_signals(self, thread_name, event):
        """Patch fcntl.flock so ``thread_name`` signals ``event`` just before it
        waits for an exclusive lock (deterministic ordering, no sleeps)."""
        import fcntl
        from unittest import mock
        real = fcntl.flock

        def flock(fd, operation):
            if (threading.current_thread().name == thread_name and operation & fcntl.LOCK_EX
                    and not operation & fcntl.LOCK_NB):
                event.set()
            return real(fd, operation)

        patcher = mock.patch.object(fcntl, "flock", side_effect=flock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_named(self, *named, timeout=60):
        results, errors = {}, {}

        def wrap(name, target):
            try:
                results[name] = target()
            except BaseException as exc:             # noqa: BLE001
                errors[name] = exc
            finally:
                connection.close()

        threads = [threading.Thread(target=wrap, args=(name, target), name=name) for name, target in named]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout)
            assert not thread.is_alive(), "deadlock"
        return results, errors

    def test_a_registration_holding_the_lock_beats_a_cleanup_past_its_precheck(self):
        execution, publication, path = self.interrupted()
        data = path.read_bytes()
        locked, cleanup_waiting = threading.Event(), threading.Event()
        self.flock_signals("cleanup", cleanup_waiting)

        def register():
            with playout._destination_lock(path):
                locked.set()
                assert cleanup_waiting.wait(20)              # the cleanup passed its pre-check and now waits
                with transaction.atomic(durable=True):
                    return self.new_track(path).pk           # committed before the lock is released

        def cleanup():
            assert locked.wait(20)
            return [(o.action, o.reasons) for o in retention.finish_retirements(apply=True).outcomes]

        results, errors = self.run_named(("register", register), ("cleanup", cleanup))
        self.assertEqual(errors, {})
        self.assertEqual(results["cleanup"], [("failed", ("path_owned_by_track",))])   # decided under the lock
        self.assertEqual(path.read_bytes(), data)                                      # the new Track's audio
        self.assertTrue(Track.objects.filter(pk=results["register"], filepath=str(path)).exists())
        self.assertNotEqual(results["register"], publication.track_id_snapshot)

    def test_a_cleanup_holding_the_lock_beats_a_registration(self):
        from unittest import mock
        execution, publication, path = self.interrupted()
        decided, register_waiting = threading.Event(), threading.Event()
        self.flock_signals("register", register_waiting)
        real_owners = retention._path_owners
        timeline = []

        def owners(destination, identity=None):
            found = real_owners(destination, identity)
            if threading.current_thread().name == "cleanup" and playout.holds_destination_lock(destination):
                decided.set()                                # the authoritative check, under the lock
                assert register_waiting.wait(20)            # the registration now waits on our lock
            return found

        def register():
            assert decided.wait(20)
            with playout._destination_lock(path):
                timeline.append(("registered", path.exists()))
                with transaction.atomic(durable=True):
                    return self.new_track(path).pk

        def cleanup():
            outcomes = retention.finish_retirements(apply=True).outcomes
            timeline.append(("cleaned", path.exists()))
            return [(o.action, o.reasons) for o in outcomes]

        with mock.patch.object(retention, "_path_owners", side_effect=owners):
            results, errors = self.run_named(("register", register), ("cleanup", cleanup))
        self.assertEqual(errors, {})
        self.assertEqual(results["cleanup"], [("removed", ())])  # decided and removed while no Track owned it
        # the registration obtained the lock only after the removal: no bytes were left for it to own
        self.assertEqual(sorted(timeline), [("cleaned", False), ("registered", False)])

    def test_two_concurrent_cleanups_remove_once(self):
        execution, publication, path = self.interrupted()
        expected = path.stat().st_size + self.wave(publication).stat().st_size
        results, errors = self.run_named(
            ("a", lambda: retention.finish_retirements(apply=True).bytes_reclaimed),
            ("b", lambda: retention.finish_retirements(apply=True).bytes_reclaimed))
        self.assertEqual(errors, {})
        self.assertFalse(path.exists())
        self.assertEqual(results["a"] + results["b"], expected)       # every byte removed (and counted) once
        self.assertFalse(self.wave(publication).exists())
        self.assertEqual(retention.finish_retirements(apply=True).outcomes, [])

    def test_an_uncoordinated_library_sync_racing_cleanup_never_owns_the_path(self):
        from library.management.commands.sync_track_file import sync_track_file
        from library.models import TrackWriteRefused
        execution, publication, path = self.interrupted()

        def sync():
            try:
                sync_track_file(str(path), ready2air=False)
            except TrackWriteRefused:
                return "refused"
            except Exception as exc:                        # noqa: BLE001 -- file already gone, etc.
                return type(exc).__name__
            return "registered"

        results, errors = self.run_named(("sync", sync),
                                         ("cleanup", lambda: retention.finish_retirements(apply=True).outcomes))
        self.assertEqual(errors, {})
        self.assertNotEqual(results["sync"], "registered")
        self.assertFalse(Track.objects.filter(filepath=str(path)).exists())
        self.assertFalse(path.exists())


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class QuarantineRaceTests(Lifecycle, TransactionTestCase):
    """The identity-preserving quarantine across PostgreSQL sessions: a
    library sync while cleanup holds the verified file aside, and a cleanup
    blocked by a conflicting occupant that is retried once it is resolved."""

    serialized_rollback = True
    interrupted = OwnershipRaceTests.interrupted
    run_named = OwnershipRaceTests.run_named

    def pause(self, stage, thread_name, reached, release):
        from unittest import mock

        from active_messages import quarantine

        def checkpoint(current):
            if current == stage and threading.current_thread().name == thread_name:
                reached.set()
                assert release.wait(20)
        patcher = mock.patch.object(quarantine, "_checkpoint", side_effect=checkpoint)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_library_sync_while_the_file_is_held_in_quarantine(self):
        from library.management.commands.sync_track_file import sync_track_file
        from library.models import TrackWriteRefused
        execution, publication, path = self.interrupted()
        size = path.stat().st_size + self.wave(publication).stat().st_size
        held, release = threading.Event(), threading.Event()
        self.pause("verified", "cleanup", held, release)

        def sync():
            assert held.wait(20)                         # the verified file is in quarantine; the name is empty
            try:
                sync_track_file(str(path), ready2air=False)
                return "registered"
            except TrackWriteRefused:
                return "refused"
            except Exception as exc:                     # noqa: BLE001 -- nothing at the name
                return type(exc).__name__
            finally:
                release.set()

        results, errors = self.run_named(
            ("cleanup", lambda: [(o.action, o.bytes_reclaimed) for o in
                                 retention.finish_retirements(apply=True).outcomes]),
            ("sync", sync))
        self.assertEqual(errors, {})
        self.assertNotEqual(results["sync"], "registered")
        self.assertEqual(results["cleanup"], [("removed", size)])
        self.assertFalse(Track._base_manager.filter(filepath__contains=".am-quarantine").exists())
        self.assertFalse(Track._base_manager.filter(filepath=str(path)).exists())
        self.assertFalse(path.exists())

    def test_a_blocked_cleanup_is_retried_after_the_conflict_is_resolved(self):
        from unittest import mock

        from active_messages import quarantine
        execution, publication, path = self.interrupted()
        aside = path.with_name("verified-original.aside")
        newest = b"an occupant that arrived during the restore"
        real_move = quarantine._rename_noreplace
        steps = []

        def move(src_dirfd, src, dst_dirfd, dst):
            if src == path.name and not steps:
                steps.append("substituted")
                path.rename(aside)
                path.write_bytes(b"unrelated replacement")
            elif src == quarantine.PAYLOAD and steps == ["substituted"]:
                steps.append("occupied")
                path.write_bytes(newest)
            return real_move(src_dirfd, src, dst_dirfd, dst)

        with mock.patch.object(quarantine, "_rename_noreplace", side_effect=move):
            (blocked,) = retention.finish_retirements(apply=True).outcomes
        self.assertEqual((blocked.action, blocked.reasons[0]), ("failed", "quarantine_restore_blocked"))
        self.assertEqual(path.read_bytes(), newest)
        self.assertIn("quarantine_restore_blocked", [f.code for f in reconcile.reconcile().findings])
        path.rename(path.with_name("operator-kept.flac"))       # the operator resolves the conflict
        outcomes = retention.recover_quarantine(apply=True).outcomes
        self.assertEqual([(o.action, o.bytes_reclaimed) for o in outcomes], [("restored", 0)])
        self.assertEqual(path.read_bytes(), b"unrelated replacement")   # put back, never deleted
        self.assertEqual(aside.read_bytes()[:4], b"fLaC")               # the verified original untouched


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class QuarantineClaimRaceTests(Lifecycle, TransactionTestCase):
    """The final ownership check -> verified deletion interval, across
    PostgreSQL sessions: a supported Track writer racing recovery either
    commits its claim first (and recovery keeps / puts back the object) or is
    refused (and recovery deletes) -- never a committed reference to a deleted
    object. Barriers, not sleeps."""

    serialized_rollback = True
    interrupted = OwnershipRaceTests.interrupted
    run_named = OwnershipRaceTests.run_named
    flock_signals = OwnershipRaceTests.flock_signals

    def held(self, publication, nth):
        """Crash the cleanup right after the ``nth`` move (1: audio, 2: waveform)."""
        from unittest import mock

        from active_messages import quarantine
        seen = []

        def checkpoint(stage):
            if stage == "after_move":
                seen.append(stage)
                if len(seen) == nth:
                    raise SystemExit("killed after the move")
        with mock.patch.object(quarantine, "_checkpoint", side_effect=checkpoint):
            try:
                retention.finish_retirements(apply=True)
            except SystemExit:
                pass
        directory = self.library_root / "UrgentPA" if nth == 1 else self.waveforms
        root = directory / quarantine.ROOT
        (operation,) = os.listdir(root)
        return root / operation / quarantine.PAYLOAD

    def pause_cleanup_at(self, stage, reached, release):
        from unittest import mock

        from active_messages import quarantine

        def checkpoint(current):
            if current == stage and threading.current_thread().name == "cleanup":
                reached.set()
                assert release.wait(20)
        patcher = mock.patch.object(quarantine, "_checkpoint", side_effect=checkpoint)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cleanup_wins_every_claim_is_refused(self):
        from library.models import TrackWriteRefused
        execution, publication, path = self.interrupted()
        payload = self.held(publication, 1)
        size = payload.stat().st_size
        alias = self.library_root.parent / "alias.flac"
        alias.symlink_to(payload)
        song = self.song("Ordinary")
        authorized, release, writer_waiting = threading.Event(), threading.Event(), threading.Event()
        self.pause_cleanup_at("authorized", authorized, release)
        self.flock_signals("writer", writer_waiting)
        refused = []

        def writer():
            assert authorized.wait(20)                    # recovery's LAST ownership check said "unowned"
            claims = {
                "create waveform_path": lambda: Track.objects.create(
                    title="w", artist=self.song_artist, filepath=str(self.library_root / "w.wav"),
                    filename="w.wav", waveform_path=str(payload)),
                "update waveform_path alias": lambda: Track.objects.filter(pk=song.pk).update(
                    waveform_path=str(alias)),
                "create filepath": lambda: Track.objects.create(
                    title="f", artist=self.song_artist, filepath=str(payload), filename="payload"),
                "register the former name": lambda: Track.objects.create(
                    title="n", artist=self.song_artist, filepath=str(path), filename=path.name),
            }
            for label, claim in claims.items():
                try:
                    with transaction.atomic():
                        claim()
                except TrackWriteRefused:
                    refused.append(label)
            release.set()
            with playout._destination_lock(path):         # a coordinated claim waits for the cleanup
                return os.path.exists(payload)

        results, errors = self.run_named(
            ("cleanup", lambda: [(o.action, o.bytes_reclaimed) for o in
                                 retention.recover_quarantine(apply=True).outcomes]),
            ("writer", writer))
        self.assertEqual(errors, {})
        self.assertEqual(sorted(refused), sorted(["create waveform_path", "update waveform_path alias",
                                                  "create filepath", "register the former name"]))
        self.assertTrue(writer_waiting.is_set())
        self.assertEqual(results["cleanup"], [("removed", size)])
        self.assertFalse(results["writer"])               # by the time it held the lock the object was gone
        self.assertFalse(payload.exists())
        self.assertEqual(retention.track_references(path, None), [])
        self.assertEqual(Track.objects.get(pk=song.pk).waveform_path, song.waveform_path)

    def test_a_waveform_claim_that_commits_first_keeps_the_waveform(self):
        execution, publication, path = self.interrupted()
        payload = self.held(publication, 2)
        wave = self.wave(publication)
        data = payload.read_bytes()
        song = self.song("Ordinary")
        locked, cleanup_waiting = threading.Event(), threading.Event()
        self.flock_signals("cleanup", cleanup_waiting)

        def writer():
            with playout._destination_lock(path):
                locked.set()
                assert cleanup_waiting.wait(20)            # recovery has begun and waits for the lock
                with transaction.atomic(durable=True):     # committed before the lock is released
                    Track.objects.filter(pk=song.pk).update(waveform_path=str(wave))

        def cleanup():
            assert locked.wait(20)
            return [(o.action, o.reasons, o.bytes_reclaimed) for o in
                    retention.recover_quarantine(apply=True).outcomes]

        results, errors = self.run_named(("writer", writer), ("cleanup", cleanup))
        self.assertEqual(errors, {})
        ((action, reasons, removed),) = results["cleanup"]
        self.assertEqual((action, removed), ("restored", 0))
        self.assertIn("waveform_owned_by_track", reasons)
        self.assertEqual(wave.read_bytes(), data)            # the Track's waveform is where it says
        self.assertEqual(Track.objects.get(pk=song.pk).waveform_path, str(wave))
