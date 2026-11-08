"""3.11: the shared guarded relocation service (library.services.media_relocation) and Track Detail's use of it.

Every file lives in a per-test temporary Library root (library.tests.test_library_reconciliation_b's fixture):
LIBRARY_ROOT, WAVEFORMS_DIR and REPORTS_ROOT point there, so no station media, cache or report is touched.  The
lock-contention and commit cases run against the disposable PostgreSQL test database in real transactions
(TransactionTestCase, one thread per connection)."""
import errno
import json
import os
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from django.db import OperationalError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext

from library.models import Category, Track
from library.services import library_reconciliation as reconciliation
from library.services import media_relocation as m
from library.services import source_paths
from library.tests.test_library_reconciliation_b import _ReconFixture

AUDIO = b"\x02relocation-audio" * 300


class _Fixture(_ReconFixture):
    def setUp(self):
        super().setUp()
        self.jazz = Category.objects.create(code="JAZZ", name="Jazz", kind=self.rock.kind)

    def put(self, rel, *, category=None, data=AUDIO, **fields):
        return self.track(rel, category=category or self.rock, data=data, **fields)

    def dest(self, track, code="ROCK"):
        return str(self.root / code / Path(track.filepath).name)

    def relocate(self, track, destination=None, *, code="ROCK", source=None, write=None, check=None, witness=None,
            strict=True, allow_copy=False, budget=1.0, protection=None, origin="reconciliation"):
        if destination is None:
            destination = self.dest(track, code)

        def default_write(locked, filepath):
            locked.filepath = filepath
            locked.save(update_fields=["filepath", "updated_at"])

        return m.execute(track_id=track.pk, source=source or track.filepath, destination=destination,
                         origin=origin, user=self.admin, write=write or default_write, check=check,
                         expected_witness=witness, strict=strict, allow_copy=allow_copy, lock_budget_seconds=budget,
                         protection=protection if protection is not None else reconciliation._protection_predicates())

    @staticmethod
    def witness(path):
        return reconciliation.stat_witness(os.lstat(path))

    def assert_untouched(self, track, result, outcome):
        self.assertEqual(result.outcome, outcome, result)
        self.assertFalse(result.committed)
        row = Track.objects.get(pk=track.pk)
        self.assertEqual(row.filepath, track.filepath)
        self.assertEqual(Path(track.filepath).read_bytes(), AUDIO)


class _Reloc(_Fixture, TestCase):
    pass


class RelocationServiceTests(_Reloc):
    def test_01_an_ordinary_move_keeps_the_inode_and_the_audio(self):
        track = self.put("POP/song.flac")
        before = os.lstat(track.filepath)
        result = self.relocate(track, witness=self.witness(track.filepath))
        destination = self.dest(track)
        self.assertEqual((result.outcome, result.committed, result.cleanup), (m.MOVED, True, m.CLEANUP_REMOVED))
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, destination)
        self.assertFalse(os.path.lexists(track.filepath))
        after = os.lstat(destination)
        self.assertEqual((after.st_dev, after.st_ino, after.st_size, after.st_nlink),
                         (before.st_dev, before.st_ino, before.st_size, 1))
        self.assertEqual(Path(destination).read_bytes(), AUDIO)
        self.assertEqual(Track.objects.get(pk=track.pk).category_id, self.rock.pk)     # nothing else changed

    def test_02_an_already_correct_destination_is_no_file_operation(self):
        track = self.put("ROCK/home.flac")
        inode = os.lstat(track.filepath).st_ino
        for destination in (track.filepath, None):
            with self.subTest(destination=destination):
                result = m.execute(track_id=track.pk, source=track.filepath, destination=destination,
                                   origin="reconciliation", user=self.admin, write=lambda t, p: None)
                self.assertEqual((result.outcome, result.cleanup), (m.UPDATED, m.CLEANUP_NOT_NEEDED))
                self.assertEqual(os.lstat(track.filepath).st_ino, inode)

    def test_03_a_destination_collision_is_never_overwritten(self):
        track = self.put("POP/clash.flac")
        other = self.file("ROCK/clash.flac", b"OTHER TRACK")
        result = self.relocate(track)
        self.assert_untouched(track, result, m.COLLISION)
        self.assertEqual(other.read_bytes(), b"OTHER TRACK")
        self.assertIn("never overwritten", result.message)

    def test_03b_a_destination_another_track_names_is_a_collision(self):
        track = self.put("POP/named.flac")
        Track.objects.create(filepath=self.dest(track), filename="named.flac", title="x", artist=self.artist,
                             category=self.rock)
        self.assert_untouched(track, self.relocate(track), m.COLLISION)

    def test_04_a_missing_source_is_stale(self):
        track = self.put("POP/gone.flac")
        os.unlink(track.filepath)
        result = self.relocate(track)
        self.assertEqual(result.outcome, m.STALE)
        self.assertFalse(os.path.lexists(self.dest(track)))

    def test_05_a_changed_source_witness_is_stale(self):
        track = self.put("POP/changed.flac")
        witness = self.witness(track.filepath)
        with open(track.filepath, "ab") as handle:
            handle.write(b"tag edit")
        result = self.relocate(track, witness=witness)
        self.assertEqual(result.outcome, m.STALE)
        self.assertFalse(os.path.lexists(self.dest(track)))
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, track.filepath)

    def test_06_a_replaced_source_inode_is_stale(self):
        track = self.put("POP/replaced.flac")
        witness = self.witness(track.filepath)
        replacement = self.file("POP/.replacement", AUDIO)
        os.replace(replacement, track.filepath)
        result = self.relocate(track, witness=witness)
        self.assertEqual(result.outcome, m.STALE)
        self.assertFalse(os.path.lexists(self.dest(track)))

    def test_07_a_symlink_source_is_refused_and_its_target_untouched(self):
        target = self.file("POP/real.flac", AUDIO)
        link = self.root / "POP" / "link.flac"
        link.symlink_to(target.name)
        track = Track.objects.create(filepath=str(link), filename="link.flac", title="l", artist=self.artist,
                                     category=self.rock)
        result = self.relocate(track)
        self.assertEqual(result.outcome, m.REFUSED)
        self.assertIn("symbolic link", result.message)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), AUDIO)
        self.assertFalse(os.path.lexists(self.dest(track)))

    def test_08_a_symlinked_source_directory_is_refused(self):
        self.file("REAL/dir.flac", AUDIO)
        (self.root / "ALIAS").symlink_to(self.root / "REAL", target_is_directory=True)
        track = Track.objects.create(filepath=str(self.root / "ALIAS" / "dir.flac"), filename="dir.flac", title="d",
                                     artist=self.artist, category=self.rock)
        result = self.relocate(track)
        self.assertEqual(result.outcome, m.REFUSED)
        self.assertTrue((self.root / "REAL" / "dir.flac").exists())
        self.assertFalse(os.path.lexists(self.dest(track)))

    def test_09_a_symlinked_destination_directory_is_refused(self):
        track = self.put("POP/escape.flac")
        with TemporaryDirectory(dir=self.base) as outside:
            (self.root / "JAZZ").symlink_to(outside, target_is_directory=True)
            result = self.relocate(track, code="JAZZ")
            self.assert_untouched(track, result, m.REFUSED)
            self.assertEqual(os.listdir(outside), [])

    def test_10_a_directory_substitution_during_staging_fails_closed(self):
        track = self.put("POP/race.flac")
        (self.root / "JAZZ").mkdir()
        real_link = os.link
        with TemporaryDirectory(dir=self.base) as outside:
            def substitute(*args, **kwargs):
                (self.root / "JAZZ").rename(self.root / "JAZZ-parked")
                (self.root / "JAZZ").symlink_to(outside, target_is_directory=True)
                return real_link(*args, **kwargs)

            with mock.patch("library.services.media_relocation.os.link", side_effect=substitute):
                result = self.relocate(track, code="JAZZ")
            self.assert_untouched(track, result, m.FAILED_BEFORE_COMMIT)
            self.assertEqual(os.listdir(outside), [])
            self.assertEqual(os.listdir(self.root / "JAZZ-parked"), [])

    def test_11_an_invalid_category_folder_is_refused(self):
        track = self.put("POP/code.flac")
        for code in ("..", "a/b", ""):
            with self.subTest(code=code), self.assertRaises(m.Stop) as caught:
                m.destination_for(track.filepath, code, str(self.root))
            self.assertEqual(caught.exception.outcome, m.REFUSED)
        for destination in (str(self.root.parent / "code.flac"), str(self.root / "a" / "b" / "code.flac"),
                            str(self.root / "ROCK" / "renamed.flac")):
            with self.subTest(destination=destination):
                self.assert_untouched(track, self.relocate(track, destination), m.REFUSED)

    def test_12_a_source_outside_the_library_is_refused(self):
        outside = self.base / "elsewhere"
        outside.mkdir()
        (outside / "far.flac").write_bytes(AUDIO)
        track = Track.objects.create(filepath=str(outside / "far.flac"), filename="far.flac", title="f",
                                     artist=self.artist, category=self.rock)
        result = self.relocate(track)
        self.assertEqual(result.outcome, m.REFUSED)
        self.assertTrue((outside / "far.flac").exists())

    def test_13_a_hard_linked_source_is_refused(self):
        track = self.put("POP/shared.flac")
        os.link(track.filepath, self.root / "POP" / "shared-alias.flac")
        self.assert_untouched(track, self.relocate(track), m.REFUSED)
        self.assertFalse(os.path.lexists(self.dest(track)))

    def test_14_protected_spoken_files_and_remote_tracks_are_refused(self):
        spoken = self.put("POP/spoken-" + "a" * 32 + ".flac")
        result = self.relocate(spoken)
        self.assertEqual(result.outcome, m.REFUSED)
        self.assertIn("spoken projection", result.message)
        remote = self.put("POP/remote.flac", remote_url="https://example.invalid/stream")
        self.assertEqual(self.relocate(remote).outcome, m.REFUSED)

    def test_15_a_file_still_in_delivery_is_refused(self):
        self.fresh.stop()
        track = self.put("POP/arriving.flac")
        result = self.relocate(track)
        self.assert_untouched(track, result, m.REFUSED)
        self.assertIn("last minute", result.message)
        self.fresh.start()

    def test_18_a_database_failure_after_staging_removes_only_the_staged_entry(self):
        track = self.put("POP/dbfail.flac")

        def failing_write(locked, filepath):
            self.assertTrue(os.path.exists(filepath))              # staged
            raise RuntimeError("injected save failure")

        result = self.relocate(track, write=failing_write)
        self.assert_untouched(track, result, m.FAILED_BEFORE_COMMIT)
        self.assertFalse(os.path.lexists(self.dest(track)))
        self.assertEqual(os.lstat(track.filepath).st_nlink, 1)

    def test_19_a_cleanup_failure_after_commit_is_reported_as_committed(self):
        track = self.put("POP/cleanup.flac")
        with mock.patch.object(m.StagedMediaRelocation, "remove_source", side_effect=OSError("injected")):
            result = self.relocate(track)
        destination = self.dest(track)
        self.assertEqual((result.outcome, result.committed, result.cleanup),
                         (m.COMMITTED_CLEANUP_REQUIRED, True, m.CLEANUP_RETAINED_CHANGED))
        self.assertTrue(result.attention)
        self.assertTrue(result.message.startswith("The move committed"))
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, destination)
        self.assertTrue(os.path.exists(destination) and os.path.exists(track.filepath))   # second entry kept
        self.assertEqual(os.lstat(destination).st_ino, os.lstat(track.filepath).st_ino)

    def test_19b_cleanup_keeps_the_old_entry_when_a_track_names_it(self):
        track = self.put("POP/owned.flac")

        def write(locked, filepath):
            locked.filepath = filepath
            locked.save(update_fields=["filepath", "updated_at"])
            Track.objects.create(filepath=track.filepath, filename="owned.flac", title="newcomer", artist=self.artist,
                                 category=self.pop)

        result = self.relocate(track, write=write)
        self.assertEqual((result.outcome, result.cleanup), (m.COMMITTED_CLEANUP_REQUIRED, m.CLEANUP_RETAINED_OWNER))
        self.assertTrue(os.path.exists(track.filepath))

    def test_20_a_cross_filesystem_move_is_refused_here_and_creates_nothing(self):
        track = self.put("POP/xdev.flac")
        with mock.patch("library.services.media_relocation.os.link",
                        side_effect=OSError(errno.EXDEV, "cross-device link")):
            result = self.relocate(track)
        self.assert_untouched(track, result, m.REFUSED)
        self.assertIn("another filesystem", result.message)
        self.assertFalse(os.path.lexists(self.dest(track)))

    def test_20b_an_interrupted_copy_fallback_leaves_the_original(self):
        track = self.put("POP/copyfail.flac")
        with mock.patch("library.services.media_relocation.os.link",
                        side_effect=OSError(errno.EXDEV, "cross-device link")), \
                mock.patch("library.services.media_relocation.os.write", side_effect=OSError(errno.ENOSPC, "full")):
            result = self.relocate(track, strict=False, allow_copy=True, origin="track_detail")
        self.assert_untouched(track, result, m.FAILED_BEFORE_COMMIT)
        self.assertFalse(os.path.lexists(self.dest(track)))

    def test_22_a_destination_created_during_staging_is_not_overwritten(self):
        track = self.put("POP/late.flac")
        real_link = os.link

        def late(src, dst, **kwargs):
            (self.root / "ROCK" / "late.flac").write_bytes(b"ARRIVED FIRST")
            return real_link(src, dst, **kwargs)

        (self.root / "ROCK").mkdir()
        with mock.patch("library.services.media_relocation.os.link", side_effect=late):
            result = self.relocate(track)
        self.assert_untouched(track, result, m.COLLISION)
        self.assertEqual((self.root / "ROCK" / "late.flac").read_bytes(), b"ARRIVED FIRST")

    def test_23_ownership_and_the_next_scan_agree_after_a_move(self):
        track = self.put("POP/agree.flac")
        self.assertEqual(self.relocate(track).outcome, m.MOVED)
        destination = self.dest(track)
        self.assertEqual(list(Track.objects.filter(filepath=destination).values_list("pk", flat=True)), [track.pk])
        self.assertFalse(Track.objects.filter(filepath=track.filepath).exists())
        report = self.scan()
        self.assertEqual(self.of(report, "category_drift"), [])
        self.assertEqual(self.of(report, "unregistered"), [])
        self.assertEqual(self.of(report, "missing"), [])

    def test_the_caller_check_runs_under_the_locks_before_any_file_operation(self):
        track = self.put("POP/check.flac")
        seen = {}

        def check(locked):
            seen["path_exists"] = os.path.lexists(self.dest(track))
            raise m.Stop(m.STALE, "the caller's evidence changed")

        result = self.relocate(track, check=check)
        self.assertEqual((result.outcome, result.message, seen), (m.STALE, "the caller's evidence changed",
                                                                   {"path_exists": False}))
        self.assertFalse(os.path.lexists(self.dest(track)))

    def test_path_locks_are_taken_before_the_row_lock_in_sorted_order(self):
        track = self.put("POP/order.flac")
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.relocate(track).outcome, m.MOVED)
        sql = [q["sql"] for q in queries.captured_queries]
        advisory = [i for i, s in enumerate(sql) if "pg_advisory_xact_lock" in s]
        row = next(i for i, s in enumerate(sql) if "FOR UPDATE" in s)
        # source and destination (sorted), the row, then Track.save's re-entrant claim and the cleanup's lock
        self.assertGreaterEqual(len(advisory), 3)
        self.assertLess(advisory[1], row)
        self.assertLess(row, advisory[2])
        keys = [source_paths.lock_key(p) for p in sorted([track.filepath, self.dest(track)])]
        self.assertIn(str(keys[0]), sql[advisory[0]])
        self.assertIn(str(keys[1]), sql[advisory[1]])

    def test_every_outcome_is_audited_with_its_origin(self):
        track = self.put("POP/audit.flac")
        with mock.patch("library.services.library_audit.record") as record:
            self.relocate(track, origin="bulk_edit")
            os.unlink(self.dest(track))
            other = self.put("POP/audit2.flac")
            os.unlink(other.filepath)
            self.relocate(other)
        outcomes = [(c.args[0], c.kwargs["outcome"], c.kwargs["detail"]["origin"]) for c in record.call_args_list]
        self.assertEqual(outcomes, [("relocation.track", "moved", "bulk_edit"),
                                    ("relocation.track", "stale", "reconciliation")])
        detail = record.call_args_list[0].kwargs["detail"]
        self.assertEqual((detail["from"], detail["to"], detail["cleanup"]), ("POP/audit.flac", "ROCK/audit.flac",
                                                                             "removed"))
        with self.assertRaises(ValueError):
            m.execute(track_id=track.pk, source="x", destination=None, origin="elsewhere", user=None,
                      write=lambda t, p: None)


# --------------------------------------------------------------------------- real transactions

class _Threads(_Fixture, TransactionTestCase):
    def thread(self, target, out, key):
        def body():
            try:
                out[key] = target()
            except BaseException as exc:          # surfaced by the assertions
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

    def holding(self, statement, params, out, release):
        """A transaction on another connection that runs ``statement`` and keeps its locks until ``release``."""
        held = threading.Event()

        def body():
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(statement, params)
                held.set()
                release.wait(20)
            return "released"
        worker = self.thread(body, out, "holder")
        self.assertTrue(held.wait(10))
        return worker


class ContentionTests(_Threads):
    def test_16_a_held_destination_path_lock_makes_the_move_busy_within_the_budget(self):
        track = self.put("POP/pathlock.flac")
        out, release = {}, threading.Event()
        holder = self.holding("SELECT pg_advisory_xact_lock(%s)", [source_paths.lock_key(self.dest(track))], out,
                              release)
        try:
            started = time.monotonic()
            result = self.relocate(track, budget=0.5)
            elapsed = time.monotonic() - started
        finally:
            release.set()
            holder.join(10)
        self.assertEqual(result.outcome, m.BUSY, result)
        self.assertLess(elapsed, 3.0)
        self.assert_untouched(track, result, m.BUSY)
        self.assertFalse(os.path.lexists(self.dest(track)))

    def test_17_a_held_track_row_makes_the_move_busy_and_changes_nothing(self):
        track = self.put("POP/rowlock.flac")
        out, release = {}, threading.Event()
        holder = self.holding("SELECT id FROM library_track WHERE id = %s FOR UPDATE", [track.pk], out, release)
        try:
            result = self.relocate(track, budget=0.5)
        finally:
            release.set()
            holder.join(10)
        self.assert_untouched(track, result, m.BUSY)
        self.assertFalse(os.path.lexists(self.dest(track)))

    def test_17b_a_registration_of_the_destination_waits_for_the_move_and_then_fails(self):
        track = self.put("POP/register.flac")
        destination = self.dest(track)
        staged, release, out = threading.Event(), threading.Event(), {}

        def slow_write(locked, filepath):
            locked.filepath = filepath
            locked.save(update_fields=["filepath", "updated_at"])
            staged.set()
            release.wait(20)

        mover = self.thread(lambda: self.relocate(track, write=slow_write, budget=5), out, "move")
        self.assertTrue(staged.wait(10))
        registrar = self.thread(lambda: Track.objects.create(filepath=destination, filename="register.flac",
                                                             title="intruder", artist=self.artist), out, "register")
        self.assertTrue(self.wait_for(lambda: self.lock_waiters() >= 1), "the registration did not wait")
        release.set()
        mover.join(20)
        registrar.join(20)
        self.assertEqual(out["move"].outcome, m.MOVED)
        self.assertIsInstance(out["register"], Exception)          # the path is the moved Track's now
        self.assertEqual(list(Track.objects.filter(filepath=destination).values_list("pk", flat=True)), [track.pk])

    def test_an_unconfirmed_commit_keeps_both_entries(self):
        track = self.put("POP/uncertain.flac")
        real_commit = connection.commit
        calls = {"n": 0}

        def flaky_commit(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                connection.rollback()
                raise OperationalError("server closed the connection unexpectedly")
            return real_commit(*args, **kwargs)

        with mock.patch.object(connection, "commit", side_effect=flaky_commit):
            result = self.relocate(track)
        self.assertEqual((result.outcome, result.cleanup), (m.UNCERTAIN, m.CLEANUP_BOTH_RETAINED))
        self.assertTrue(result.attention)
        self.assertTrue(os.path.exists(track.filepath) and os.path.exists(self.dest(track)))
        self.assertIn(Track.objects.get(pk=track.pk).filepath, (track.filepath, self.dest(track)))


# --------------------------------------------------------------------------- Track Detail uses the service

class TrackDetailTests(_Reloc):
    def patch_track(self, track, body):
        return self.client.patch(f"/api/tracks/{track.pk}/", data=json.dumps(body), content_type="application/json")

    def test_track_detail_relocates_through_the_shared_service(self):
        track = self.put("POP/detail.flac", category=self.pop)
        with mock.patch.object(m, "execute", wraps=m.execute) as spy:
            response = self.patch_track(track, {"category_id": self.jazz.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(spy.call_args.kwargs["origin"], "track_detail")
        self.assertEqual((spy.call_args.kwargs["strict"], spy.call_args.kwargs["allow_copy"]), (False, True))
        row = Track.objects.get(pk=track.pk)
        self.assertEqual((row.category_id, row.filepath), (self.jazz.pk, self.dest(track, "JAZZ")))
        self.assertFalse(os.path.lexists(track.filepath))

    def test_choosing_the_folders_category_no_longer_fails(self):
        # r0129: the file already in the new Category's folder made the hard link collide with itself (409).
        track = self.put("JAZZ/home.flac", category=self.pop)
        inode = os.lstat(track.filepath).st_ino
        response = self.patch_track(track, {"category_id": self.jazz.pk})
        self.assertEqual(response.status_code, 200, response.content)
        row = Track.objects.get(pk=track.pk)
        self.assertEqual((row.category_id, row.filepath), (self.jazz.pk, track.filepath))
        self.assertEqual(os.lstat(track.filepath).st_ino, inode)

    def test_a_concurrent_category_change_is_refused_not_misfiled(self):
        track = self.put("POP/racing.flac", category=self.pop)
        real = m.execute

        def concurrent(**kwargs):
            Track.objects.filter(pk=track.pk).update(category=self.rock)
            return real(**kwargs)

        with mock.patch.object(m, "execute", side_effect=concurrent):
            response = self.patch_track(track, {"category_id": self.jazz.pk})
        self.assertEqual(response.status_code, 409, response.content)
        row = Track.objects.get(pk=track.pk)
        self.assertEqual((row.category_id, row.filepath), (self.rock.pk, track.filepath))
        self.assertFalse(os.path.lexists(self.dest(track, "JAZZ")))

    def test_a_cleanup_failure_is_reported_as_committed_with_the_r0112_message(self):
        track = self.put("POP/tdcleanup.flac", category=self.pop)
        with mock.patch.object(m.StagedMediaRelocation, "remove_source", side_effect=OSError("injected")):
            response = self.patch_track(track, {"category_id": self.jazz.pk})
        self.assertEqual(response.status_code, 500)
        self.assertIn("Track update committed", response.json()["error"])
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, self.dest(track, "JAZZ"))

    def test_the_view_module_keeps_the_staging_names(self):
        from library import views
        self.assertIs(views._stage_media_relocation_no_replace, m.stage_no_replace)
        self.assertIs(views._StagedMediaRelocation, m.StagedMediaRelocation)
