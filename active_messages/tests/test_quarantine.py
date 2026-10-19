"""D3E-C final correction: identity-preserving removal (active_messages.quarantine)
on disposable filesystem fixtures -- no database, real processes.

Every destructive step acts on the object that was verified, never on
whatever a shared name denotes later: the name is moved (RENAME_NOREPLACE)
into a private operation directory, the MOVED inode is proven to be the
verified one, and only then is it removed -- with a zero-link proof. Every
substitution, crash and race below is injected at an exact step through the
module's seams (``_checkpoint``, ``_rename_noreplace``) and synchronized by
events, never by sleeps.
"""
import errno
import hashlib
import multiprocessing
import os
import stat
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from active_messages import playout, quarantine

CONTEXT = multiprocessing.get_context("fork")
WAIT = 20
NAME = "am-" + "ab" * 16 + ".flac"
CONTENT = bytes(range(256)) * 64                         # 16 KiB "publication"
OTHER = b"an unrelated file!!!"                         # what an unrelated writer leaves at the name


def expected(data=CONTENT):
    return quarantine.Expected(hashlib.sha256(data).hexdigest(), len(data))


def open_dir(path) -> int:
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def reclaim(directory: Path, name=NAME, *, data=CONTENT, authorize=lambda *_: "", want=None):
    """One cleanup exactly as retention runs it: under the destination lock,
    through a directory descriptor."""
    with playout._destination_lock(directory / name):
        fd = open_dir(directory)
        try:
            return quarantine.reclaim(fd, name, expected=want or expected(data), authorize=authorize)
        finally:
            os.close(fd)


def held(directory: Path):
    fd = open_dir(directory)
    try:
        return [(item.name, item.code, item.size) for item in quarantine.inspect(fd)]
    finally:
        os.close(fd)


def operations(directory: Path):
    root = directory / quarantine.ROOT
    return sorted(os.listdir(root)) if root.exists() else []


class _Seams:
    """Patch the module's seams in THIS process for one test."""

    def __init__(self, test):
        self.test, self.at = test, {}
        self.real_move = quarantine._rename_noreplace
        self.on_move = None
        for target, side in ((quarantine, "_checkpoint"), (quarantine, "_rename_noreplace")):
            patcher = mock.patch.object(target, side, side_effect=getattr(self, side))
            patcher.start()
            test.addCleanup(patcher.stop)

    def _checkpoint(self, stage):
        action = self.at.pop(stage, None)
        if action is not None:
            action()

    def _rename_noreplace(self, src_dirfd, src, dst_dirfd, dst):
        if self.on_move is not None:
            action, self.on_move = self.on_move, None
            action(src, dst)
        return self.real_move(src_dirfd, src, dst_dirfd, dst)


class FixtureMixin:
    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory(prefix="am-quarantine-")
        self.addCleanup(holder.cleanup)
        self.dir = Path(holder.name) / "UrgentPA"
        self.dir.mkdir()
        self.path = self.dir / NAME
        self.path.write_bytes(CONTENT)
        self.inode = os.lstat(self.path).st_ino
        self.aside = self.dir / "verified-original.aside"

    def substitute(self, payload=OTHER):
        """The verified original is renamed aside and an unrelated file takes the name."""
        os.rename(self.path, self.aside)
        self.path.write_bytes(payload)

    def sidecar_inode(self):
        return os.lstat(playout.lock_sidecar(self.path)).st_ino

    def assert_clean_quarantine(self):
        self.assertEqual(operations(self.dir), [])


class IdentityTests(FixtureMixin, SimpleTestCase):
    def test_the_verified_file_is_moved_and_removed(self):
        removed, problems = reclaim(self.dir)
        self.assertEqual((removed, problems), (len(CONTENT), []))
        self.assertFalse(self.path.exists())
        self.assert_clean_quarantine()
        root = os.lstat(self.dir / quarantine.ROOT)
        self.assertTrue(stat.S_ISDIR(root.st_mode))
        self.assertEqual(stat.S_IMODE(root.st_mode), 0o700)            # private, application-controlled
        self.assertTrue(playout.lock_sidecar(self.path).is_file())      # the lock is persistent
        self.assertEqual(reclaim(self.dir), (0, []))                    # idempotent: counted once

    def test_a_replacement_that_moves_instead_is_put_back_never_deleted(self):
        seams = _Seams(self)
        seams.on_move = lambda src, dst: self.substitute()             # right before the destructive move
        removed, problems = reclaim(self.dir)
        self.assertEqual((removed, problems), (0, ["quarantine_identity_mismatch"]))
        self.assertEqual(self.path.read_bytes(), OTHER)                 # restored under its name, intact
        self.assertEqual(self.aside.read_bytes(), CONTENT)              # the verified original untouched
        self.assert_clean_quarantine()

    def test_a_byte_identical_replacement_is_still_a_different_object(self):
        """SHA-256 and size match the expected bytes, but the inode is not the verified one."""
        seams = _Seams(self)
        seams.on_move = lambda src, dst: self.substitute(CONTENT)
        removed, problems = reclaim(self.dir)
        self.assertEqual((removed, problems), (0, ["quarantine_identity_mismatch"]))
        self.assertNotEqual(os.lstat(self.path).st_ino, self.inode)
        self.assertEqual(self.path.read_bytes(), CONTENT)
        self.assertEqual(os.lstat(self.aside).st_ino, self.inode)

    def test_the_inode_changes_while_the_operation_is_prepared(self):
        seams = _Seams(self)
        seams.at["quarantine_created"] = lambda: self.substitute(CONTENT)
        self.assertEqual(reclaim(self.dir), (0, ["quarantine_identity_mismatch"]))
        self.assertTrue(self.path.is_file() and self.aside.is_file())
        self.assert_clean_quarantine()

    def test_restoration_never_overwrites_a_new_occupant(self):
        seams = _Seams(self)
        seams.on_move = lambda src, dst: self.substitute()
        newest = b"the newest occupant of the name"
        seams.at["restoring"] = lambda: self.path.write_bytes(newest)
        removed, problems = reclaim(self.dir)
        self.assertEqual((removed, problems), (0, ["quarantine_restore_blocked"]))
        self.assertEqual(self.path.read_bytes(), newest)                # never overwritten
        (operation,) = operations(self.dir)
        payload = self.dir / quarantine.ROOT / operation / quarantine.PAYLOAD
        self.assertEqual(payload.read_bytes(), OTHER)                   # kept, never deleted
        self.assertEqual(held(self.dir), [(NAME, "quarantine_restore_blocked", len(OTHER))])
        # nothing new starts while the operation is unresolved; retried once the conflict is resolved
        self.assertEqual(reclaim(self.dir, data=newest), (0, ["quarantine_restore_blocked"]))
        self.assertEqual(self.path.read_bytes(), newest)
        os.rename(self.path, self.dir / "operator-kept-this")
        removed, problems = reclaim(self.dir)
        self.assertEqual(problems, ["quarantine_identity_mismatch", "file_identity_mismatch"])
        self.assertEqual((removed, self.path.read_bytes()), (0, OTHER))  # put back, not deleted
        self.assert_clean_quarantine()

    def test_rename_noreplace_never_replaces(self):
        fd = open_dir(self.dir)
        self.addCleanup(os.close, fd)
        (self.dir / "other").write_bytes(OTHER)
        with self.assertRaises(FileExistsError):
            quarantine._rename_noreplace(fd, NAME, fd, "other")
        self.assertEqual(((self.dir / "other").read_bytes(), self.path.read_bytes()), (OTHER, CONTENT))

    def test_an_existing_operation_directory_is_never_reused(self):
        root = self.dir / quarantine.ROOT
        root.mkdir(mode=0o700)
        token = "0" * 32
        foreign = root / f"{NAME}.{token}"
        foreign.mkdir(mode=0o700)
        (foreign / quarantine.PAYLOAD).write_bytes(OTHER)
        with mock.patch.object(quarantine.secrets, "token_hex", return_value=token), \
                mock.patch.object(quarantine, "recover", return_value=[]):
            self.assertEqual(reclaim(self.dir), (0, ["quarantine_conflict"]))
        self.assertEqual((foreign / quarantine.PAYLOAD).read_bytes(), OTHER)
        self.assertEqual(self.path.read_bytes(), CONTENT)

    def test_an_unexpected_quarantine_root_is_refused(self):
        victim = self.dir.parent / "victim"
        victim.mkdir()
        cases = {
            "symlink": lambda root: root.symlink_to(victim),
            "regular file": lambda root: root.write_bytes(b"x"),
            "fifo": lambda root: os.mkfifo(root),
            "shared directory": lambda root: (root.mkdir(), root.chmod(0o755)),
        }
        for label, make in cases.items():
            with self.subTest(label):
                root = self.dir / quarantine.ROOT
                make(root)
                removed, problems = reclaim(self.dir)
                self.assertEqual(removed, 0)
                self.assertIn(problems[0], ("quarantine_entry_unexpected", "quarantine_not_private",
                                            "quarantine_unavailable"))
                self.assertEqual(self.path.read_bytes(), CONTENT)
                self.assertEqual(list(victim.iterdir()), [])
                root.rmdir() if root.is_dir() and not root.is_symlink() else root.unlink()

    def test_unexpected_objects_at_the_name_are_refused(self):
        self.path.unlink()
        target = self.dir.parent / "target.flac"
        target.write_bytes(CONTENT)
        cases = {
            "symlink": lambda: self.path.symlink_to(target),
            "directory": lambda: self.path.mkdir(),
            "fifo": lambda: os.mkfifo(self.path),
        }
        for label, make in cases.items():
            with self.subTest(label):
                make()
                removed, problems = reclaim(self.dir)
                self.assertEqual((removed, problems), (0, ["file_not_regular"]))
                self.assertTrue(os.path.lexists(self.path))
                self.assertEqual(target.read_bytes(), CONTENT)
                self.assert_clean_quarantine()
                self.path.rmdir() if self.path.is_dir() and not self.path.is_symlink() else self.path.unlink()

    def test_a_hard_linked_file_is_never_removed_or_counted(self):
        other_name = self.dir.parent / "elsewhere.flac"
        os.link(self.path, other_name)
        self.assertEqual(reclaim(self.dir), (0, ["file_hard_linked"]))
        self.assertEqual((self.path.read_bytes(), other_name.read_bytes()), (CONTENT, CONTENT))

    def test_a_hard_link_made_after_verification_is_put_back(self):
        seams = _Seams(self)
        other_name = self.dir.parent / "elsewhere.flac"
        seams.at["before_move"] = lambda: os.link(self.path, other_name)
        self.assertEqual(reclaim(self.dir), (0, ["file_hard_linked"]))
        self.assertEqual(os.lstat(self.path).st_nlink, 2)
        self.assert_clean_quarantine()

    def test_same_size_different_sha256_is_refused(self):
        self.path.write_bytes(bytes(len(CONTENT)))
        self.assertEqual(reclaim(self.dir), (0, ["file_identity_mismatch"]))
        self.assertEqual(self.path.read_bytes(), bytes(len(CONTENT)))
        self.assert_clean_quarantine()

    def test_contents_changed_in_place_after_the_move_are_put_back(self):
        seams = _Seams(self)

        def rewrite():
            (operation,) = operations(self.dir)
            with open(self.dir / quarantine.ROOT / operation / quarantine.PAYLOAD, "r+b") as handle:
                handle.write(b"X")                                       # same inode, other bytes
        seams.at["after_move"] = rewrite
        self.assertEqual(reclaim(self.dir), (0, ["quarantine_identity_mismatch"]))
        self.assertEqual(self.path.read_bytes()[:1], b"X")
        self.assertEqual(os.lstat(self.path).st_ino, self.inode)

    def test_a_substitute_inside_the_private_directory_claims_nothing(self):
        """Outside the supported-writer contract (only this account can enter
        the 0700 directory): the zero-link proof still refuses to report it."""
        seams = _Seams(self)

        def swap_payload():
            (operation,) = operations(self.dir)
            payload = self.dir / quarantine.ROOT / operation / quarantine.PAYLOAD
            os.rename(payload, self.aside)
            payload.write_bytes(CONTENT)
        seams.at["verified"] = swap_payload
        removed, problems = reclaim(self.dir)
        self.assertEqual((removed, problems), (0, ["quarantine_substituted"]))
        self.assertEqual(os.lstat(self.aside).st_ino, self.inode)        # the verified object survives
        self.assertEqual(held(self.dir), [(NAME, "quarantine_operation_leftover", 0)])

    def test_an_unsupported_filesystem_refuses_before_moving(self):
        with mock.patch.object(quarantine, "_rename_noreplace", side_effect=OSError(errno.ENOSYS, "no")):
            self.assertEqual(reclaim(self.dir), (0, ["quarantine_unsupported"]))
        self.assertEqual(self.path.read_bytes(), CONTENT)
        self.assert_clean_quarantine()

    def test_authority_withdrawn_after_the_move_puts_the_file_back(self):
        answers = iter(["", "path_owned_by_track"])
        self.assertEqual(reclaim(self.dir, authorize=lambda *_: next(answers)), (0, ["path_owned_by_track"]))
        self.assertEqual(os.lstat(self.path).st_ino, self.inode)
        self.assert_clean_quarantine()

    def test_authority_unknown_after_the_move_keeps_it_pending_then_finishes(self):
        answers = iter(["", "ownership_unavailable"])
        self.assertEqual(reclaim(self.dir, authorize=lambda *_: next(answers)), (0, ["quarantine_pending"]))
        self.assertFalse(self.path.exists())
        self.assertEqual(held(self.dir), [(NAME, "quarantine_pending", len(CONTENT))])
        self.assertEqual(reclaim(self.dir), (len(CONTENT), []))          # re-proven, removed, counted once
        self.assert_clean_quarantine()

    def test_inspection_changes_nothing(self):
        answers = iter(["", "ownership_unavailable"])
        reclaim(self.dir, authorize=lambda *_: next(answers))

        def snapshot():
            return sorted((str(p), os.lstat(p).st_ino, os.lstat(p).st_mtime_ns) for p in self.dir.rglob("*"))
        before = snapshot()
        for _ in range(3):
            held(self.dir)
        self.assertEqual(snapshot(), before)


# -- crash boundaries: a real child process is killed inside a transition ----------------------------------

def _child(directory, stop_at, reached, substitute, recover_only):
    """A cleanup process that freezes at ``stop_at`` (until killed)."""
    import threading
    path = Path(directory) / NAME

    def checkpoint(stage):
        if stage == stop_at:
            reached.set()
            threading.Event().wait()                              # frozen: the parent kills us here

    quarantine._checkpoint = checkpoint
    if substitute:
        real = quarantine._rename_noreplace

        def move(src_dirfd, src, dst_dirfd, dst):
            if src == NAME and not os.path.lexists(Path(directory) / "verified-original.aside"):
                os.rename(path, Path(directory) / "verified-original.aside")
                path.write_bytes(OTHER)
            return real(src_dirfd, src, dst_dirfd, dst)
        quarantine._rename_noreplace = move
    if recover_only:
        with playout._destination_lock(path):
            fd = open_dir(directory)
            quarantine.recover(fd, NAME, expected=expected(), authorize=lambda *_: "")
    else:
        reclaim(Path(directory))
    os._exit(0)


class CrashRecoveryTests(FixtureMixin, SimpleTestCase):
    def kill_at(self, stage, *, substitute=False, recover_only=False):
        reached = CONTEXT.Event()
        process = CONTEXT.Process(target=_child, args=(str(self.dir), stage, reached, substitute, recover_only))
        process.start()
        self.addCleanup(lambda: (process.is_alive() and process.kill(), process.join(WAIT)))
        self.assertTrue(reached.wait(WAIT), f"the child never reached {stage}")
        process.kill()
        process.join(WAIT)
        self.assertEqual(process.exitcode, -9)                     # SIGKILL inside the transition

    def recover(self, **kwargs):
        with playout._destination_lock(self.path):
            fd = open_dir(self.dir)
            try:
                return [(s.state, s.bytes_removed, s.problem, s.detail)
                        for s in quarantine.recover(fd, NAME, **{"expected": expected(), "authorize": lambda *_: "",
                                                                 **kwargs})]
            finally:
                os.close(fd)

    def test_killed_before_creating_the_quarantine(self):
        self.kill_at("before_quarantine")
        self.assertEqual((os.lstat(self.path).st_ino, operations(self.dir)), (self.inode, []))
        self.assertEqual(reclaim(self.dir), (len(CONTENT), []))

    def test_killed_after_creating_the_operation_and_immediately_before_the_move(self):
        for stage in ("quarantine_created", "before_move"):
            with self.subTest(stage):
                self.path.write_bytes(CONTENT)
                self.inode = os.lstat(self.path).st_ino
                self.kill_at(stage)
                self.assertEqual(os.lstat(self.path).st_ino, self.inode)      # nothing moved
                self.assertEqual(held(self.dir), [(NAME, "quarantine_operation_leftover", 0)])
                self.assertEqual(self.recover(), [("cleared", 0, "", "abandoned_before_move")])
                self.assertEqual(reclaim(self.dir), (len(CONTENT), []))
                self.assert_clean_quarantine()

    def test_killed_after_the_move_or_after_verification(self):
        for stage in ("after_move", "verified"):
            with self.subTest(stage):
                self.path.write_bytes(CONTENT)
                self.kill_at(stage)
                self.assertFalse(self.path.exists())
                self.assertEqual(held(self.dir), [(NAME, "quarantine_pending", len(CONTENT))])
                # recovery re-proves identity from scratch, then removes and counts -- once
                self.assertEqual(reclaim(self.dir), (len(CONTENT), []))
                self.assertEqual(reclaim(self.dir), (0, []))
                self.assert_clean_quarantine()

    def test_killed_after_deletion_before_reporting(self):
        self.kill_at("deleted")
        self.assertFalse(self.path.exists())
        self.assertEqual(held(self.dir), [(NAME, "quarantine_operation_leftover", 0)])
        self.assertEqual(self.recover(), [("cleared", 0, "", "concluded")])   # never counted twice
        self.assert_clean_quarantine()

    def test_killed_while_restoring_a_mismatched_file(self):
        self.kill_at("restoring", substitute=True)
        self.assertFalse(self.path.exists())
        self.assertEqual(held(self.dir), [(NAME, "quarantine_identity_mismatch", len(OTHER))])
        self.assertEqual(self.recover(), [("restored", 0, "quarantine_identity_mismatch",
                                           "quarantine_identity_mismatch")])
        self.assertEqual(self.path.read_bytes(), OTHER)
        self.assertEqual(self.aside.read_bytes(), CONTENT)
        self.assert_clean_quarantine()

    def test_killed_during_a_recovery_sweep(self):
        self.kill_at("after_move")
        self.kill_at("verified", recover_only=True)                # the recovering process dies too
        self.assertEqual(held(self.dir), [(NAME, "quarantine_pending", len(CONTENT))])
        self.assertEqual(self.recover(), [("deleted", len(CONTENT), "", "")])
        self.assert_clean_quarantine()

    def test_recovery_never_deletes_an_unproven_object(self):
        self.kill_at("after_move")
        # 1. content authority disagrees (the database expects other bytes): put back, never deleted
        self.assertEqual(self.recover(expected=expected(OTHER))[0][:3], ("restored", 0, "quarantine_identity_mismatch"))
        self.assertEqual(self.path.read_bytes(), CONTENT)
        # 2. no provable identity (manifest lost): kept for an operator
        self.kill_at("after_move")
        (operation,) = operations(self.dir)
        manifest = self.dir / quarantine.ROOT / operation / quarantine.MANIFEST
        payload = self.dir / quarantine.ROOT / operation / quarantine.PAYLOAD
        manifest.write_bytes(b"{not json")
        self.assertEqual(self.recover(), [("kept", 0, "quarantine_recovery_required", "")])
        self.assertEqual(payload.read_bytes(), CONTENT)
        self.assertEqual(held(self.dir), [(NAME, "quarantine_recovery_required", len(CONTENT))])
        # 3. content authority unavailable: pending, kept
        manifest.unlink()
        self.assertEqual(self.recover(expected=None, authorize=lambda *_: "ownership_unavailable")[0][:3],
                         ("kept", 0, "quarantine_recovery_required"))
        self.assertTrue(payload.is_file())

    def test_an_old_operation_is_never_deleted_because_it_is_old(self):
        self.kill_at("after_move")
        (operation,) = operations(self.dir)
        os.utime(self.dir / quarantine.ROOT / operation / quarantine.PAYLOAD, (0, 0))
        self.assertEqual(self.recover(expected=None, authorize=lambda *_: "ownership_unavailable"),
                         [("kept", 0, "quarantine_pending", "ownership_unavailable")])


# -- races between independent processes ------------------------------------------------------------------

def _racer(directory, name, start, result, index, blocked=None, paused_at=None, resume=None):
    import fcntl
    if blocked is not None:
        real = fcntl.flock

        def flock(fd, operation):
            if operation & fcntl.LOCK_EX and not operation & fcntl.LOCK_NB:
                blocked.set()
            return real(fd, operation)
        fcntl.flock = flock
    if paused_at is not None:
        def checkpoint(stage):
            if stage in paused_at:
                paused_at[stage].set()
                resume.wait(WAIT)
        quarantine._checkpoint = checkpoint
    start.wait(WAIT)
    removed, problems = reclaim(Path(directory), name)
    result[index] = removed
    os._exit(0 if not problems else 3)


def _publisher(directory, blocked, placed):
    """Publish's critical section: place a NEW file at the destination under its lock."""
    import fcntl
    real = fcntl.flock

    def flock(fd, operation):
        if operation & fcntl.LOCK_EX and not operation & fcntl.LOCK_NB:
            blocked.set()
        return real(fd, operation)
    fcntl.flock = flock
    path = Path(directory) / NAME
    with playout._destination_lock(path):
        scratch = Path(directory) / ".am-publish-test"
        scratch.write_bytes(b"freshly published audio")
        os.rename(scratch, path)
        placed.value = os.lstat(path).st_ino
    os._exit(0)


class ProcessRaceTests(FixtureMixin, SimpleTestCase):
    def start(self, target, *args):
        process = CONTEXT.Process(target=target, args=args)
        process.start()
        self.addCleanup(lambda: (process.is_alive() and process.kill(), process.join(WAIT)))
        return process

    def test_two_processes_recover_the_same_operation_once(self):
        CrashRecoveryTests.kill_at(self, "after_move")
        lock_inode = self.sidecar_inode()
        result = CONTEXT.Array("q", 2)
        start, a_paused, b_blocked, resume = CONTEXT.Event(), CONTEXT.Event(), CONTEXT.Event(), CONTEXT.Event()
        a = self.start(_racer, str(self.dir), NAME, start, result, 0, None, {"recovering": a_paused}, resume)
        start.set()
        self.assertTrue(a_paused.wait(WAIT))                              # A holds the lock, recovering
        b = self.start(_racer, str(self.dir), NAME, start, result, 1, b_blocked)
        self.assertTrue(b_blocked.wait(WAIT))                             # B waits on the same lock
        resume.set()
        for process in (a, b):
            process.join(WAIT)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sorted(result[:]), [0, len(CONTENT)])           # removed and counted exactly once
        self.assertEqual(self.sidecar_inode(), lock_inode)
        self.assert_clean_quarantine()

    def test_a_substitution_by_another_process_during_cleanup(self):
        lock_inode = self.sidecar_inode() if playout.lock_sidecar(self.path).exists() else None
        result = CONTEXT.Array("q", 1)
        start, paused, resume = CONTEXT.Event(), CONTEXT.Event(), CONTEXT.Event()
        cleaner = self.start(_racer, str(self.dir), NAME, start, result, 0, None, {"before_move": paused}, resume)
        start.set()
        self.assertTrue(paused.wait(WAIT))                                # verified; about to move
        self.substitute()                                                 # an independent writer
        resume.set()
        cleaner.join(WAIT)
        self.assertEqual((cleaner.exitcode, result[0]), (3, 0))
        self.assertEqual(self.path.read_bytes(), OTHER)
        self.assertEqual(os.lstat(self.aside).st_ino, self.inode)
        self.assert_clean_quarantine()
        if lock_inode is not None:
            self.assertEqual(self.sidecar_inode(), lock_inode)

    def test_a_publication_of_the_same_destination_waits_for_the_cleanup(self):
        result, placed = CONTEXT.Array("q", 1), CONTEXT.Value("q", 0)
        start, paused, resume, blocked = CONTEXT.Event(), CONTEXT.Event(), CONTEXT.Event(), CONTEXT.Event()
        cleaner = self.start(_racer, str(self.dir), NAME, start, result, 0, None, {"verified": paused}, resume)
        start.set()
        self.assertTrue(paused.wait(WAIT))                                # payload verified, in quarantine
        lock_inode = self.sidecar_inode()
        publisher = self.start(_publisher, str(self.dir), blocked, placed)
        self.assertTrue(blocked.wait(WAIT))                               # publish waits on the same lock
        self.assertFalse(self.path.exists())
        resume.set()
        cleaner.join(WAIT)
        publisher.join(WAIT)
        self.assertEqual((cleaner.exitcode, publisher.exitcode, result[0]), (0, 0, len(CONTENT)))
        self.assertEqual(self.path.read_bytes(), b"freshly published audio")   # the new file is untouched
        self.assertEqual(os.lstat(self.path).st_ino, placed.value)
        self.assertEqual(self.sidecar_inode(), lock_inode)
        self.assert_clean_quarantine()
