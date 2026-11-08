"""3.11 corrections (Codex re-review, Critical A and B): the relocation's fixed destination identity and its
identity-safe removal, under deterministic fault injection.

Every case uses real files in a per-test temporary Library root and checks what actually survives (bytes, inodes),
the database row and the reported outcome.  The interleavings are injected at the filesystem calls of
``library.services.media_relocation``: a competing writer that takes no IsadoraAir lock replaces an entry with an
unrelated recording of exactly the same length, a different inode and copied timestamps."""
import errno
import json
import os
from pathlib import Path
from unittest import mock

from django.test import TestCase

from library.models import Category, Track
from library.services import library_reconciliation as reconciliation
from library.services import media_relocation as m
from library.tests.test_library_category_drift import _DriftFixture
from library.tests.test_library_bulk_relocation import _BulkFixture

AUTHORIZED = b"AUTHORIZED-AUDIO" * 64
UNRELATED = b"UNRELATED-AUDIO!" * 64            # the same length


def _everywhere(root, data):
    found = []
    for directory, _dirs, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            if not path.is_symlink() and path.read_bytes() == data:
                found.append(os.path.relpath(path, root))
    return sorted(found)


class _Identity(_DriftFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.real_stat, self.real_link, self.real_unlink = os.stat, os.link, os.unlink
        self.real_rename = m._rename_noreplace

    def put(self, rel="POP/song.flac", category=None):
        return self.track(rel, category=category or self.rock, data=AUTHORIZED)

    def dest(self, track, code="ROCK"):
        return self.root / code / Path(track.filepath).name

    def substitute(self, path, data=UNRELATED):
        temp = Path(path).with_name(".sub-" + Path(path).name)
        temp.write_bytes(data)
        if os.path.lexists(path):
            info = self.real_stat(path, follow_symlinks=False)
            os.utime(temp, ns=(info.st_atime_ns, info.st_mtime_ns))     # copied timestamps
        os.rename(temp, path)

    def relocate(self, track, *, write=None, strict=True, allow_copy=False, code="ROCK"):
        def default_write(locked, filepath):
            locked.filepath = filepath
            locked.save(update_fields=["filepath", "updated_at"])
        return m.execute(track_id=track.pk, source=track.filepath, destination=str(self.dest(track, code)),
                         origin="reconciliation", user=self.admin, write=write or default_write, strict=strict,
                         allow_copy=allow_copy, protection=reconciliation._protection_predicates())

    def assert_not_committed(self, track, result):
        self.assertFalse(result.committed, result)
        self.assertNotIn(result.outcome, m.COMMITTED, result)
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, track.filepath)
        self.assertEqual(Path(track.filepath).read_bytes(), AUTHORIZED)
        self.assertEqual(_everywhere(self.root, UNRELATED), ["ROCK/song.flac"], "the unrelated file was not kept")

    def quarantine_holders(self):
        q = self.root / m.QUARANTINE_DIRNAME
        return sorted(p.name for p in q.iterdir()) if q.exists() else []


# --------------------------------------------------------------------------- Critical A: the destination identity

class DestinationIdentityTests(_Identity):
    def test_a1_substituted_immediately_after_the_link(self):
        track = self.put()

        def link(*args, **kwargs):
            self.real_link(*args, **kwargs)
            self.substitute(self.dest(track))

        with mock.patch("os.link", side_effect=link):
            result = self.relocate(track)
        self.assertEqual(result.outcome, m.FAILED_BEFORE_COMMIT)
        self.assert_not_committed(track, result)

    def test_a2_substituted_while_the_source_is_revalidated(self):
        track = self.put()
        state = {"linked": False, "done": False}

        def link(*args, **kwargs):
            self.real_link(*args, **kwargs)
            state["linked"] = True

        def stat(path, *args, **kwargs):
            info = self.real_stat(path, *args, **kwargs)
            if state["linked"] and not state["done"] and os.fsdecode(path) == "song.flac" and kwargs.get("dir_fd"):
                state["seen"] = state.get("seen", 0) + 1
                if state["seen"] == 2:                         # 1: the check right after the link; 2: the source
                    state["done"] = True
                    self.substitute(self.dest(track))
            return info

        with mock.patch("os.link", side_effect=link), mock.patch("os.stat", side_effect=stat):
            result = self.relocate(track)
        self.assertTrue(state["done"])
        self.assert_not_committed(track, result)

    def test_a3_substituted_after_the_database_write_before_the_final_binding_check(self):
        track = self.put()

        def write(locked, filepath):
            locked.filepath = filepath
            locked.save(update_fields=["filepath", "updated_at"])
            self.substitute(filepath)

        result = self.relocate(track, write=write)
        self.assertEqual(result.outcome, m.FAILED_BEFORE_COMMIT)
        self.assert_not_committed(track, result)

    def test_a4_substituted_immediately_before_the_database_write(self):
        track = self.put()

        def write(locked, filepath):
            self.substitute(filepath)
            locked.filepath = filepath
            locked.save(update_fields=["filepath", "updated_at"])

        result = self.relocate(track, write=write)
        self.assert_not_committed(track, result)

    def test_a5_a_substituted_source_path_is_not_authority(self):
        track = self.put()

        def write(locked, filepath):
            self.substitute(track.filepath)                    # the source path now names another recording
            locked.filepath = filepath
            locked.save(update_fields=["filepath", "updated_at"])

        result = self.relocate(track, write=write)
        self.assertEqual((result.outcome, result.committed, result.cleanup),
                         (m.FAILED_BEFORE_COMMIT, False, m.CLEANUP_DESTINATION_RETAINED))
        self.assertTrue(result.attention)
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, track.filepath)
        self.assertEqual(Path(track.filepath).read_bytes(), UNRELATED)   # the writer's file is untouched
        # The staged link is now the authorized recording's only name: kept, never rolled back into oblivion.
        self.assertEqual(self.dest(track).read_bytes(), AUTHORIZED)
        self.assertIn("only remaining name", result.cleanup_message)

    def test_a6_copy_fallback_destination_substituted_while_it_is_written(self):
        track = self.put()
        state = {"done": False}
        real_write = os.write

        def write(fd, data):
            count = real_write(fd, data)
            if not state["done"]:
                state["done"] = True
                self.substitute(self.dest(track))
            return count

        with mock.patch("os.link", side_effect=OSError(errno.EXDEV, "cross-device")), \
                mock.patch("os.write", side_effect=write):
            result = self.relocate(track, strict=False, allow_copy=True)
        self.assertTrue(state["done"])
        self.assert_not_committed(track, result)

    def test_a7_a_copy_that_does_not_read_back_as_the_source_is_refused_and_removed(self):
        track = self.put()
        real_pread = os.pread

        def pread(fd, size, offset):
            data = real_pread(fd, size, offset)
            return data[:-1] + b"X" if data else data

        with mock.patch("os.link", side_effect=OSError(errno.EXDEV, "cross-device")), \
                mock.patch("os.pread", side_effect=pread):
            result = self.relocate(track, strict=False, allow_copy=True)
        self.assertEqual(result.outcome, m.FAILED_BEFORE_COMMIT)
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, track.filepath)
        self.assertFalse(os.path.lexists(self.dest(track)))         # our own copy, identity-checked, removed
        self.assertEqual(self.quarantine_holders(), [])

    def test_a8_an_ordinary_copy_fallback_still_commits_a_verified_copy(self):
        track = self.put()
        with mock.patch("os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
            result = self.relocate(track, strict=False, allow_copy=True)
        self.assertEqual((result.outcome, result.cleanup), (m.MOVED, m.CLEANUP_REMOVED))
        self.assertEqual(self.dest(track).read_bytes(), AUTHORIZED)
        self.assertFalse(os.path.lexists(track.filepath))


# --------------------------------------------------------------------------- Critical B: removal

class RemovalTests(_Identity):
    def rename_hook(self, when, action):
        """Run ``action`` once, just before the first atomic move whose (source name) matches ``when``."""
        state = {"done": False}

        def rename(src_fd, src, dst_fd, dst):
            if not state["done"] and when(os.fsdecode(src), os.fsdecode(dst)):
                state["done"] = True
                action()
            return self.real_rename(src_fd, src, dst_fd, dst)
        return mock.patch.object(m, "_rename_noreplace", side_effect=rename), state

    def test_b1_and_b3_a_replacement_during_the_rollback_is_never_deleted(self):
        track = self.put()

        def failing(locked, filepath):
            self.substitute(filepath)                         # replaced, then the database write fails
            raise RuntimeError("injected database failure")

        result = self.relocate(track, write=failing)
        self.assertEqual(result.outcome, m.FAILED_BEFORE_COMMIT)
        self.assert_not_committed(track, result)
        self.assertNotEqual(result.cleanup, m.CLEANUP_DESTINATION_RETAINED)   # nothing of ours remains there

    def test_b2_and_b8_a_replacement_between_inspection_and_removal_of_the_old_source(self):
        track = self.put()
        hook, state = self.rename_hook(lambda src, dst: src == "song.flac" and dst == "entry",
                                       lambda: self.substitute(track.filepath))
        with hook:
            result = self.relocate(track)
        self.assertTrue(state["done"])
        self.assertEqual((result.outcome, result.committed, result.cleanup),
                         (m.COMMITTED_CLEANUP_REQUIRED, True, m.CLEANUP_RETAINED_CHANGED))
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, str(self.dest(track)))
        self.assertEqual(self.dest(track).read_bytes(), AUTHORIZED)
        self.assertEqual(Path(track.filepath).read_bytes(), UNRELATED)        # restored, untouched
        self.assertEqual(self.quarantine_holders(), [])

    def test_b4_and_b5_an_external_file_at_the_old_source_after_commit_is_kept(self):
        for case in ("replaced", "recreated"):
            with self.subTest(case=case):
                track = self.put(f"POP/{case}.flac")
                real = m._remove_old_entry

                def before_cleanup(staged, source, using):
                    if case == "replaced":
                        self.substitute(source)
                    else:
                        os.unlink(source)
                        Path(source).write_bytes(UNRELATED)
                    return real(staged, source, using)

                with mock.patch.object(m, "_remove_old_entry", side_effect=before_cleanup):
                    result = self.relocate(track)
                self.assertEqual((result.outcome, result.committed), (m.COMMITTED_CLEANUP_REQUIRED, True))
                self.assertEqual(Path(track.filepath).read_bytes(), UNRELATED)
                self.assertEqual(Path(self.dest(track)).read_bytes(), AUTHORIZED)

    def test_b6_a_track_registered_at_the_old_source_keeps_its_file(self):
        track = self.put()
        real = m._remove_old_entry

        def register(staged, source, using):
            Track.objects.create(filepath=source, filename="song.flac", title="newcomer", artist=self.artist)
            return real(staged, source, using)

        with mock.patch.object(m, "_remove_old_entry", side_effect=register):
            result = self.relocate(track)
        self.assertEqual((result.outcome, result.cleanup), (m.COMMITTED_CLEANUP_REQUIRED, m.CLEANUP_RETAINED_OWNER))
        self.assertTrue(os.path.exists(track.filepath))

    def test_b9_a_mismatch_whose_name_was_taken_again_stays_in_quarantine_with_a_manifest(self):
        track = self.put()
        occupant = b"THIRD-FILE-AT-THE-OLD-NAME"
        real = self.real_rename
        state = {"substituted": False}

        def rename(src_fd, src, dst_fd, dst):
            src, dst = os.fsdecode(src), os.fsdecode(dst)
            if src == "song.flac" and dst == "entry" and not state["substituted"]:
                state["substituted"] = True
                self.substitute(track.filepath)            # replaced just before it is moved aside
            if src == "entry" and dst == "song.flac":
                Path(track.filepath).write_bytes(occupant)  # and its name is taken again before the restore
            return real(src_fd, src, dst_fd, dst)

        with mock.patch.object(m, "_rename_noreplace", side_effect=rename):
            result = self.relocate(track)
        self.assertEqual((result.outcome, result.cleanup), (m.COMMITTED_CLEANUP_REQUIRED, m.CLEANUP_QUARANTINED))
        self.assertTrue(result.attention)
        retained = Path(result.cleanup_location)
        self.assertEqual(retained.read_bytes(), UNRELATED)                  # kept, never deleted
        self.assertEqual(Path(track.filepath).read_bytes(), occupant)      # never overwritten
        manifest = json.loads((retained.parent / "RETAINED.json").read_text())
        self.assertEqual(manifest["source"], track.filepath)
        self.assertIn("not restorable", manifest["reason"])
        self.assertEqual(result.as_dict()["cleanup_location"], os.path.relpath(retained, self.root))
        self.assertEqual(self.dest(track).read_bytes(), AUTHORIZED)

    def test_b10_an_unusable_quarantine_removes_nothing(self):
        cases = {
            "not private": lambda: (self.root / m.QUARANTINE_DIRNAME).mkdir(mode=0o777) or
            os.chmod(self.root / m.QUARANTINE_DIRNAME, 0o777),
            "a symlink": lambda: (self.root / m.QUARANTINE_DIRNAME).symlink_to(self.base, target_is_directory=True),
            "no renameat2": None,
        }
        for name, prepare in cases.items():
            with self.subTest(case=name):
                q = self.root / m.QUARANTINE_DIRNAME
                if q.is_symlink():
                    q.unlink()
                elif q.exists():
                    q.rmdir()
                track = self.put(f"POP/{name.replace(' ', '-')}.flac")
                if prepare:
                    prepare()
                    result = self.relocate(track)
                else:
                    with mock.patch.object(m, "_RENAMEAT2", None):
                        result = self.relocate(track)
                self.assertEqual((result.outcome, result.cleanup),
                                 (m.COMMITTED_CLEANUP_REQUIRED, m.CLEANUP_RETAINED_UNSAFE))
                self.assertTrue(os.path.exists(track.filepath))            # left in place
                self.assertEqual(os.lstat(track.filepath).st_ino, os.lstat(self.dest(track)).st_ino)

    def test_b10b_a_rollback_with_an_unusable_quarantine_keeps_the_staged_link_and_says_so(self):
        track = self.put()
        with mock.patch.object(m, "_RENAMEAT2", None):
            result = self.relocate(track, write=lambda locked, filepath: (_ for _ in ()).throw(RuntimeError("db")))
        self.assertEqual((result.outcome, result.cleanup), (m.FAILED_BEFORE_COMMIT, m.CLEANUP_DESTINATION_RETAINED))
        self.assertTrue(result.attention)
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, track.filepath)
        self.assertTrue(os.path.exists(self.dest(track)) and os.path.exists(track.filepath))

    def test_b11_an_interrupted_removal_leaves_a_recoverable_quarantined_entry(self):
        track = self.put()

        def unlink(path, *args, **kwargs):
            if os.fsdecode(path) == "entry":
                raise OSError(errno.EIO, "injected I/O error")
            return self.real_unlink(path, *args, **kwargs)

        with mock.patch("os.unlink", side_effect=unlink):
            result = self.relocate(track)
        self.assertEqual((result.outcome, result.cleanup), (m.COMMITTED_CLEANUP_REQUIRED, m.CLEANUP_QUARANTINED))
        retained = Path(result.cleanup_location)
        self.assertEqual(os.lstat(retained).st_ino, os.lstat(self.dest(track)).st_ino)   # an extra link of ours
        self.assertIn("interrupted", json.loads((retained.parent / "RETAINED.json").read_text())["reason"])
        self.assertEqual(self.dest(track).read_bytes(), AUTHORIZED)

    def test_b12_an_ordinary_move_leaves_no_old_entry_and_no_quarantine_holder(self):
        track = self.put()
        result = self.relocate(track)
        self.assertEqual((result.outcome, result.cleanup), (m.MOVED, m.CLEANUP_REMOVED))
        self.assertFalse(os.path.lexists(track.filepath))
        self.assertEqual(os.lstat(self.dest(track)).st_nlink, 1)
        self.assertEqual(self.quarantine_holders(), [])
        self.assertEqual(oct(os.lstat(self.root / m.QUARANTINE_DIRNAME).st_mode & 0o777), "0o700")
        report = self.scan()                                   # the hidden quarantine is not walked
        self.assertEqual((self.of(report, "unregistered"), self.of(report, "category_drift")), ([], []))

    def test_the_old_source_is_never_removed_once_the_committed_destination_is_gone(self):
        track = self.put()
        real = m._remove_old_entry

        def destination_deleted(staged, source, using):
            os.unlink(staged.destination)
            return real(staged, source, using)

        with mock.patch.object(m, "_remove_old_entry", side_effect=destination_deleted):
            result = self.relocate(track)
        self.assertEqual(result.cleanup, m.CLEANUP_RETAINED_UNSAFE)
        self.assertEqual(Path(track.filepath).read_bytes(), AUTHORIZED)       # never the last copy


# --------------------------------------------------------------------------- through the three callers

class CallerTests(_BulkFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.real_link = os.link

    def substitute_after_link(self, name):
        def link(src, dst, *args, **kwargs):
            self.real_link(src, dst, *args, **kwargs)
            if os.fsdecode(dst) == name:
                folder = kwargs["dst_dir_fd"]
                temp = f".sub-{name}"
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=folder)
                os.write(fd, UNRELATED)
                os.close(fd)
                os.rename(temp, name, src_dir_fd=folder, dst_dir_fd=folder)
        return mock.patch("os.link", side_effect=link)

    def check(self, track, destination):
        self.assertEqual(Track.objects.get(pk=track.pk).filepath, track.filepath)
        self.assertEqual(Path(track.filepath).read_bytes(), AUTHORIZED)
        self.assertEqual(Path(destination).read_bytes(), UNRELATED)

    def test_track_detail(self):
        track = self.track("POP/detail.flac", category=self.pop, data=AUTHORIZED)
        with self.substitute_after_link("detail.flac"):
            response = self.client.patch(f"/api/tracks/{track.pk}/", data=json.dumps({"category_id": self.jazz.pk}),
                                         content_type="application/json")
        self.assertEqual(response.status_code, 500, response.content)
        self.assertEqual(Track.objects.get(pk=track.pk).category_id, self.pop.pk)
        self.check(track, self.root / "JAZZ" / "detail.flac")

    def test_reconciliation(self):
        track = self.track("POP/drift.flac", category=self.rock, data=AUTHORIZED)
        self.engine()
        report = reconciliation.run_scan()
        finding = next(f for f in report["findings"] if f["type"] == "category_drift" and f["track"]["id"] == track.pk)
        self.engine()
        status, body = self.post("library:api-reconciliation-drift-preview", {
            "scan_id": report["scan_id"], "selections": [{"finding_id": finding["id"], "direction": "move_file"}]})
        token = body["items"][0]["token"]
        self.engine()
        with self.substitute_after_link("drift.flac"):
            status, body = self.post("library:api-reconciliation-drift-apply", {"token": token, "confirm": True})
        self.assertEqual(body["item"]["outcome"], m.FAILED_BEFORE_COMMIT)
        self.check(track, self.root / "ROCK" / "drift.flac")

    def test_bulk_editor(self):
        track = self.track("ROCK/bulk.flac", category=self.rock, data=AUTHORIZED)
        status, body = self.preview(self.ids(track), self.to(self.jazz), "move")
        with self.substitute_after_link("bulk.flac"):
            status, result = self.post("library:api-track-bulk-edit-apply", {
                "selection": self.ids(track), "changes": self.to(self.jazz), "file_location": "move",
                "confirm_move": True, "token": body["token"]})
        self.assertEqual([f["id"] for f in result["failed"]], [track.pk])
        self.assertEqual(Track.objects.get(pk=track.pk).category_id, self.rock.pk)
        self.check(track, self.root / "JAZZ" / "bulk.flac")
